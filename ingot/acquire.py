"""Fetch remote package bytes without admitting, reviewing, or executing them."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ingot.optimize.tree import MAX_FILES, MAX_TREE_BYTES, portable_path

_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$")
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")


def _remote_url(repository: str) -> str:
    return f"https://github.com/{repository}.git"


def _git(*args: str) -> bytes:
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(["git", *args], capture_output=True, env=environment,
                                timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"Git acquisition failed: {error}") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Git acquisition failed: {detail or 'git exited non-zero'}")
    return result.stdout


def _resolved_commit(remote: str, ref: str) -> str:
    output = _git("ls-remote", remote, ref)
    rows = [line.split(b"\t", 1) for line in output.splitlines()]
    matches = [sha.decode("ascii") for sha, name in rows
               if name.decode("utf-8", errors="replace") == ref]
    if len(matches) != 1 or not _COMMIT.fullmatch(matches[0]):
        raise ValueError(f"Git acquisition failed: ref {ref!r} did not resolve to one commit")
    return matches[0]


def _bounded_tree(repository: Path, subdirectory: str) -> list[tuple[str, str, str, int]]:
    """Return safe entries after enforcing bounds, before asking Git for blob contents."""
    output = _git("-C", str(repository), "ls-tree", "-r", "-l", "-z", "HEAD", "--",
                  subdirectory)
    entries, total = [], 0
    prefix = f"{subdirectory}/"
    for record in output.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, object_id, raw_size = metadata.split()
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("Git acquisition failed: the selected tree has an invalid entry") \
                from error
        if not path.startswith(prefix):
            raise ValueError(f"Git acquisition failed: {path!r} escapes the selected package")
        relative = path[len(prefix):]
        portable_path(relative, allow_skill_md=True)
        if mode == b"120000":
            raise ValueError(f"symlinks are not admissible: {relative}")
        if kind != b"blob" or raw_size == b"-":
            raise ValueError(f"not a regular file: {relative}")
        size = int(raw_size)
        entries.append((relative, mode.decode("ascii"), object_id.decode("ascii"), size))
        total += size

    if not entries:
        raise ValueError(f"Git acquisition failed: {subdirectory!r} is not a package directory")
    if len(entries) > MAX_FILES:
        raise ValueError(f"a package may hold at most {MAX_FILES} files; this one holds "
                         f"{len(entries)}")
    if total > MAX_TREE_BYTES:
        raise ValueError(f"a package may hold at most {MAX_TREE_BYTES} bytes; this one holds {total}")
    return entries


def _materialize(repository: Path, package: Path,
                 entries: list[tuple[str, str, str, int]]) -> None:
    """Write raw Git blobs, bypassing checkout hooks and attribute-selected filters."""
    for relative, mode, object_id, expected_size in entries:
        content = _git("-C", str(repository), "cat-file", "blob", object_id)
        if len(content) != expected_size:
            raise ValueError(f"Git acquisition failed: {relative} changed while it was fetched")
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o755 if mode == "100755" else 0o644)


def github(repository: str, *, ref: str, subdirectory: str,
           destination: Path) -> tuple[Path, dict]:
    """Fetch one public GitHub repository subdirectory at an exact resolved commit."""
    if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository) or \
            repository.casefold().endswith(".git"):
        raise ValueError(f"GitHub repository must be OWNER/REPO, found {repository!r}")
    if ref != "HEAD":
        raise ValueError("GitHub acquisition currently supports the remote HEAD ref")
    selected = portable_path(subdirectory).as_posix()
    remote = _remote_url(repository)
    commit = _resolved_commit(remote, ref)

    destination = Path(destination)
    clone = destination / "repository"
    destination.mkdir(parents=True, exist_ok=True)
    _git("-c", "core.hooksPath=/dev/null", "-c", "core.symlinks=false", "clone",
         "--depth", "1", "--filter=blob:none", "--no-checkout", "--no-tags",
         "--single-branch", remote, str(clone))
    checked_out = _git("-C", str(clone), "rev-parse", "HEAD").decode("ascii").strip()
    if checked_out != commit:
        raise ValueError(
            f"Git acquisition failed: ref moved from {commit} to {checked_out} during acquisition")

    entries = _bounded_tree(clone, selected)
    package = destination / "package"
    _materialize(clone, package, entries)
    return package, {"repository": repository, "ref": ref, "commit": commit,
                     "subdirectory": selected}
