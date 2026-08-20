"""The exact bytes of an admitted package, staged for publication.

Admission used to reduce a package to a dictionary of decoded text components. Anything that
dictionary could not hold -- an image, a PDF, a wheel, a file whose bytes are not valid UTF-8 --
was dropped on the way in, and the revision the reviewer approved then named a package that was
missing them. A content-addressed release controller has exactly one promise to keep: a revision
names the exact package. Dropping a file breaks it silently, which is the worst way to break it.

So a candidate is a *tree*, not a dictionary. Every regular file is copied byte-for-byte into a
staging directory named by the tree's digest, and the manifest records each file's relative path,
mode, size, and SHA-256 of its raw bytes. Publication copies that staged tree into the vault
worktree and verifies every hash on the way. The reviewer, the receipt, and the vault are then
looking at the same bytes.

Two deliberate exceptions, both visible rather than silent:

- **SKILL.md is normalized, not preserved.** Its frontmatter is the routing interface: the name is
  forced to the skill's identity, the description is collapsed to one line, and the whole thing is
  re-emitted through a safe YAML dump so a stray `---` in a model's output cannot corrupt it. The
  manifest still records the source file's real hash and size, so the normalization is auditable,
  but what gets served is the normalized file. `revision()` below hashes the result of doing
  exactly that, so the approved revision is the served revision.
- **Symlinks are refused.** Preserving one means the vault commits a link that a reader follows out
  of the library; flattening one into its target silently changes the artifact's shape. Neither is
  a decision admission should make on an operator's behalf, so a package containing a symlink is
  refused by name until there is a reason to build one of them.

Modes are clamped to 0o644 or 0o755 because those are the only two a Git checkout reproduces, and
the vault is a Git repository. Recording the raw mode would describe bytes the vault cannot serve.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unicodedata
import uuid
from pathlib import Path, PurePosixPath

from ingot import paths

TREE_SCHEMA = "ingot/candidate-tree/v1"

# Bounds on what admission will stage. The byte limit does the real work; the file count stops a
# package of a million empty files from turning one approval into a filesystem problem. Both sit
# above the advisory thresholds `ingot review` warns at, so a package it merely warns about is
# still admissible.
MAX_FILES = 256
MAX_TREE_BYTES = 20_000_000

_CHUNK = 1 << 20
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                     *(f"lpt{i}" for i in range(1, 10))}


def candidates_dir() -> Path:
    """Staged candidate trees. Resolved per call, never bound at import."""
    return paths.runs() / "candidates"


def staged_dir(digest: str) -> Path:
    if not isinstance(digest, str) or len(digest) != 64 or not all(
            char in "0123456789abcdef" for char in digest):
        raise ValueError(f"invalid candidate tree digest: {digest!r}")
    return candidates_dir() / digest


def portable_path(raw: str, *, allow_skill_md: bool = False) -> PurePosixPath:
    """One relative path every filesystem in the deployment agrees on.

    Shared with the text-component path in `ingot.optimize.ingress` so a package cannot enter through one
    door what the other would refuse."""
    if not isinstance(raw, str) or "\\" in raw or any(ord(char) < 32 for char in raw):
        raise ValueError(f"component is not a portable POSIX path: {raw}")
    path = PurePosixPath(raw)
    parts = path.parts
    forbidden = {"."} if allow_skill_md else {".", "SKILL.md"}
    if path.is_absolute() or ".." in parts or path.as_posix() in forbidden:
        raise ValueError(f"component escapes skill root: {raw}")
    if any(":" in part or part.endswith((".", " ")) or
           part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED for part in parts):
        raise ValueError(f"component is not a portable POSIX path: {raw}")
    if any(unicodedata.normalize("NFC", part) != part for part in parts):
        raise ValueError(f"component path must be NFC-normalized: {raw}")
    return path


def _hash(path: Path) -> tuple[str, int]:
    """SHA-256 of the raw bytes, and the byte count. Never a decoded string: a file that is not
    valid UTF-8 has no decoded form, and one that is would hash differently after a round trip."""
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _mode(raw: int) -> int:
    return 0o755 if raw & 0o111 else 0o644


def _digest(files: list[dict]) -> str:
    canonical = json.dumps({"schema_version": TREE_SCHEMA, "files": files},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _walk(root: Path, current: Path) -> list[Path]:
    """Every regular file under `current`, refusing anything that is not one.

    An explicit descent rather than `rglob`, which follows directory symlinks: a package could
    otherwise pull in a whole tree from outside itself and this would never see the link."""
    found = []
    for item in sorted(current.iterdir()):
        relative = item.relative_to(root).as_posix()
        if item.is_symlink():
            raise ValueError(f"symlinks are not admissible: {relative}")
        if item.is_dir():
            found.extend(_walk(root, item))
        elif item.is_file():
            found.append(item)
        else:
            raise ValueError(f"not a regular file: {relative}")
    return found


def build(package: Path) -> dict:
    """Describe every regular file in `package` exactly as it will be served."""
    package = Path(package).resolve()
    files, total, folded = [], 0, set()
    for path in _walk(package, package):
        raw = path.relative_to(package).as_posix()
        relative = portable_path(raw, allow_skill_md=True).as_posix()
        if relative.casefold() in folded:
            raise ValueError(f"component path collides case-insensitively: {raw}")
        folded.add(relative.casefold())
        checksum, size = _hash(path)
        total += size
        files.append({"path": relative, "mode": _mode(path.stat().st_mode),
                      "size": size, "sha256": checksum})

    if not files:
        raise ValueError("the package holds no files")
    if len(files) > MAX_FILES:
        raise ValueError(f"a package may hold at most {MAX_FILES} files; this one holds "
                         f"{len(files)}")
    if total > MAX_TREE_BYTES:
        raise ValueError(f"a package may hold at most {MAX_TREE_BYTES} bytes; this one holds "
                         f"{total}")
    files.sort(key=lambda entry: entry["path"])
    return {"schema_version": TREE_SCHEMA, "files": files, "size": total,
            "digest": _digest(files)}


def verify_manifest(manifest: object) -> dict:
    """The manifest is well formed and its digest covers the file list it arrived with.

    Recomputed rather than trusted, because the digest is what binds a receipt to a staged tree: a
    receipt whose file list was edited without its digest would otherwise publish a tree nobody
    approved."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != TREE_SCHEMA:
        raise ValueError("unsupported candidate tree manifest")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("a candidate tree manifest lists at least one file")
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("candidate tree entries must be objects")
        if set(entry) != {"path", "mode", "size", "sha256"}:
            raise ValueError(f"unexpected candidate tree entry: {sorted(entry)}")
        portable_path(entry["path"], allow_skill_md=True)
        if entry["mode"] not in (0o644, 0o755):
            raise ValueError(f"unsupported mode for {entry['path']}: {entry['mode']}")
        if not isinstance(entry["size"], int) or isinstance(entry["size"], bool) \
                or entry["size"] < 0:
            raise ValueError(f"invalid size for {entry['path']}")
    if _digest(files) != manifest.get("digest"):
        raise ValueError("candidate tree digest does not cover its file list")
    return manifest


def stage(package: Path, manifest: dict) -> Path:
    """Copy the described bytes somewhere the publisher can reach them.

    The source directory belongs to whoever ran `ingot add`; it can be edited or deleted between
    the approval and the publication, and a publisher that read from it would publish whatever it
    found. Staging is named by the tree digest, so ingesting identical bytes twice converges on one
    directory instead of racing."""
    package = Path(package).resolve()
    destination = staged_dir(manifest["digest"])
    if destination.is_dir():
        return destination

    candidates_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = candidates_dir() / f".{manifest['digest']}.{uuid.uuid4().hex}.tmp"
    try:
        for entry in manifest["files"]:
            target = temporary / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(package / entry["path"], target)
            os.chmod(target, entry["mode"])
            checksum, size = _hash(target)
            if (checksum, size) != (entry["sha256"], entry["size"]):
                raise ValueError(f"{entry['path']} changed while it was being staged")
        try:
            os.rename(temporary, destination)
        except OSError:
            # Another submitter staged the identical tree first. Same digest, same bytes.
            if not destination.is_dir():
                raise
            shutil.rmtree(temporary, ignore_errors=True)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def materialize(manifest: dict, destination: Path) -> None:
    """Write the staged tree into `destination`, checking every file against the manifest."""
    verify_manifest(manifest)
    source = staged_dir(manifest["digest"])
    if not source.is_dir():
        raise ValueError(f"the staged candidate tree {manifest['digest'][:16]} is missing")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for entry in manifest["files"]:
        relative = portable_path(entry["path"], allow_skill_md=True)
        staged = source / relative
        checksum, size = _hash(staged)
        if (checksum, size) != (entry["sha256"], entry["size"]):
            raise ValueError(f"staged candidate file does not match the receipt: {entry['path']}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(staged, target)
        os.chmod(target, entry["mode"])


def materialize_creation(manifest: dict, components: dict, skill: str, destination: Path) -> None:
    """The whole tree, then the normalized SKILL.md over the top.

    The one implementation of what an admitted package becomes. `revision()` runs it into a
    temporary directory to compute the revision a reviewer approves, and the publisher runs it into
    the vault worktree to produce the revision the library serves. They cannot disagree, because
    there is nothing here for them to disagree about."""
    from ingot.mcp_server.registry import write_skill_md

    materialize(manifest, destination)
    try:
        metadata = json.loads(components.get("frontmatter") or "{}")
    except (TypeError, ValueError) as exc:
        raise ValueError("creation frontmatter is not valid JSON") from exc
    if not isinstance(metadata, dict):
        raise ValueError("creation frontmatter is not an object")
    metadata["name"] = skill
    metadata["description"] = components["description"]
    write_skill_md(Path(destination) / "SKILL.md", metadata, components["body"])


def revision(skill: str, manifest: dict, components: dict) -> str:
    """The revision the library will serve once this tree is published.

    Computed by materializing it, because a revision derived some other way would be a second
    description of the same bytes and the two would eventually disagree."""
    from ingot.mcp_server.registry import skill_revision

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / skill
        materialize_creation(manifest, components, skill, root)
        return skill_revision(root)
