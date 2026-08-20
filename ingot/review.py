"""Deterministic, offline, read-only review of one skill package.

Six sections, never one number. A composite score invites exactly the reward-hacking the evidence
gate exists to prevent, and it hides which of six unrelated questions actually failed.

What this command will not do:

- run a model, read a key, reach the network, or start a service;
- convert similarity to another skill into an "activation score" -- that measures collision, not
  whether the router loads this skill at the right moment;
- pattern-match for markers and call the result prompt-injection safety;
- write anything, anywhere.

Where a question needs evidence this command cannot produce, it reports UNMEASURED and names the
command that can. An honest gap beats a confident guess."""
from __future__ import annotations

import codecs
import json
import re
import unicodedata
from pathlib import Path

import yaml

from ingot.mcp_server.registry import SLUG_RE, skill_revision, skill_sources

from .parse import ERROR, INFO, WARNING, Finding, parse_raw

REVIEW_SCHEMA = "ingot/review/v1"

MEASURED = "measured"
UNMEASURED = "unmeasured"

# Thresholds are advisory and deliberately loose: they exist to surface a package that will surprise
# someone, not to impose a house style.
LARGE_BODY_BYTES = 40_000
LARGE_PACKAGE_BYTES = 10_000_000
MANY_FILES = 200

# A relative link in the body that points at something in the package. Skips URLs and anchors.
_LOCAL_LINK = re.compile(r"\[[^\]]*\]\(\s*(?!\w+:|#)([^)\s]+)")
_URL = re.compile(r"https?://[^\s<>\"')\]]+")
# Mutable by construction: a branch ref rather than a commit or tag.
_MUTABLE_REF = re.compile(r"https?://[^\s]*/(?:blob|tree|raw)/(?:main|master|HEAD)/")
_WINDOWS_RESERVED = set('<>:"|?*')


def _section(status: str = MEASURED, **extra) -> dict:
    return {"status": status, "findings": [], **extra}


def _add(section: dict, *findings: Finding) -> None:
    section["findings"].extend(finding.as_dict() for finding in findings)


def _package_files(package: Path) -> tuple[list[Path], list[Finding]]:
    """Every regular file in the package, plus a finding for any symlink.

    Symlinks are refused rather than resolved. Admission stages a package as exact bytes, and there
    are only two things it could do with a link: preserve it, which puts a path into the vault that
    a reader follows back out of the library, or flatten it into a copy of its target, which
    silently changes the artifact's shape. Neither is a call to make on an operator's behalf, so a
    package containing one is refused by name. This walks explicitly instead of using `rglob`,
    which follows directory symlinks and would pull in a subtree without ever reporting the link."""
    files, findings = [], []
    stack = [package]
    while stack:
        for path in sorted(stack.pop().iterdir()):
            if path.is_symlink():
                findings.append(Finding(
                    "symlink-unsupported", ERROR,
                    "symlinks are not admissible: admission stages exact bytes, and a link is "
                    "neither preserved nor followed",
                    path.relative_to(package).as_posix()))
            elif path.is_dir():
                stack.append(path)
            elif path.is_file():
                files.append(path)
    return sorted(files), findings


# Metadata no packaging format treats as skill content, and nothing a reviewer needs flagged.
IGNORED_PARTS = {".git", "__pycache__", ".pytest_cache"}
IGNORED_NAMES = {".DS_Store", "Thumbs.db"}


def binary_assets(package: Path, files: list[Path]) -> list[str]:
    """Files whose bytes are not text, so a reviewer knows what they are approving unread.

    Admission preserves these byte-for-byte, which is why this is a note rather than a refusal. It
    is still the fact a reviewer most needs in front of them: a skill's text can be read before it
    is approved, and a compiled binary or an image cannot. Decodability, not the file extension,
    decides -- an extension is a claim about a file, and the point here is to check the file."""
    found = []
    for path in files:
        relative = path.relative_to(package)
        if relative.name in IGNORED_NAMES or IGNORED_PARTS.intersection(relative.parts):
            continue
        # Decoded in chunks rather than read whole: this runs before any size limit applies, and a
        # review command that a large file can exhaust memory on is one nobody runs on the packages
        # that most need reviewing.
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1 << 20):
                    decoder.decode(chunk)
            decoder.decode(b"", final=True)
        except (UnicodeDecodeError, OSError):
            found.append(relative.as_posix())
    return found


def _structural(package: Path, files: list[Path], escapes: list[Finding]) -> tuple[dict, dict | None]:
    section = _section()
    _add(section, *escapes)

    skill_md = package / "SKILL.md"
    if not skill_md.is_file():
        _add(section, Finding("skill-md-missing", ERROR,
                              "no SKILL.md: a skill package is a directory containing one"))
        return section, None

    raw = parse_raw(skill_md.read_text(encoding="utf-8", errors="replace"))
    _add(section, *raw.findings)
    frontmatter = raw.frontmatter or {}

    name = str(frontmatter.get("name") or "").strip()
    if raw.frontmatter is not None:
        if not name:
            _add(section, Finding("name-missing", ERROR, "frontmatter declares no name"))
        elif not SLUG_RE.fullmatch(name):
            _add(section, Finding(
                "name-invalid", ERROR,
                f"name {name!r} is not a valid slug (lowercase letters, digits and hyphens, "
                f"starting with a letter or digit)"))
        elif name != package.name:
            _add(section, Finding(
                "name-directory-mismatch", WARNING,
                f"frontmatter name {name!r} differs from the directory name {package.name!r}; "
                f"the frontmatter name is the identity that will be served"))

        if not str(frontmatter.get("description") or "").strip():
            _add(section, Finding(
                "description-empty", ERROR,
                "no description: the router keys on it, so a skill without one is never loaded"))

    body_bytes = len(raw.body.encode("utf-8"))
    if not raw.body.strip():
        _add(section, Finding("body-empty", WARNING, "the body is empty"))
    elif body_bytes > LARGE_BODY_BYTES:
        _add(section, Finding(
            "body-large", WARNING,
            f"body is {body_bytes} bytes; every load pays for it in the agent's context"))

    total_bytes = sum(path.stat().st_size for path in files)
    if len(files) > MANY_FILES:
        _add(section, Finding("file-count-high", WARNING,
                              f"{len(files)} files in the package"))
    if total_bytes > LARGE_PACKAGE_BYTES:
        _add(section, Finding("package-large", WARNING,
                              f"package is {total_bytes} bytes"))

    _add(section, *_path_findings(package, files))
    _add(section, *_reference_findings(package, raw.body))
    binaries = binary_assets(package, files)
    if binaries:
        _add(section, Finding(
            "binary-asset", WARNING,
            f"{len(binaries)} file(s) are not text and cannot be read before approval; they will "
            f"be published byte-for-byte: {', '.join(binaries[:10])}"
            + (f", and {len(binaries) - 10} more" if len(binaries) > 10 else "")))

    section.update(
        name=name or package.name,
        description=str(frontmatter.get("description") or "").strip(),
        body_bytes=body_bytes,
        file_count=len(files),
        total_bytes=total_bytes,
        file_types=sorted({path.suffix.lower() or "(none)" for path in files}),
        binary_assets=binaries,
    )
    return section, frontmatter if raw.frontmatter is not None else None


def _path_findings(package: Path, files: list[Path]) -> list[Finding]:
    """Portability of the paths themselves: characters, case, and Unicode form.

    A pair of files that differ only by case or only by Unicode normalization is two files on Linux
    and one on macOS or Windows. That makes the package's content revision depend on who checked it
    out, which is the one property a content-addressed system cannot tolerate."""
    findings, by_fold, by_norm = [], {}, {}
    for path in files:
        relative = path.relative_to(package).as_posix()
        if _WINDOWS_RESERVED.intersection(relative) or "\\" in relative:
            findings.append(Finding(
                "path-not-portable", WARNING,
                "path contains characters that are not portable across filesystems", relative))
        by_fold.setdefault(relative.casefold(), []).append(relative)
        by_norm.setdefault(unicodedata.normalize("NFC", relative), []).append(relative)

    for group in by_fold.values():
        if len(group) > 1:
            findings.append(Finding(
                "path-case-collision", WARNING,
                f"paths differ only by case and collapse on a case-insensitive filesystem: "
                f"{', '.join(sorted(group))}"))
    for group in by_norm.values():
        if len(group) > 1 and len({unicodedata.normalize("NFC", p) for p in group}) == 1 \
                and len(set(group)) > 1:
            findings.append(Finding(
                "path-unicode-collision", WARNING,
                f"paths differ only by Unicode normalization form: {', '.join(sorted(group))}"))
    return findings


def _reference_findings(package: Path, body: str) -> list[Finding]:
    """Local links in the body: do they escape the package, and do they resolve?"""
    findings = []
    for target in _LOCAL_LINK.findall(body):
        cleaned = target.split("#", 1)[0].strip()
        if not cleaned:
            continue
        candidate = Path(cleaned)
        if candidate.is_absolute() or ".." in candidate.parts:
            findings.append(Finding(
                "path-traversal", ERROR,
                "reference points outside the package", cleaned))
            continue
        if not (package / candidate).exists():
            findings.append(Finding(
                "file-reference-missing", WARNING,
                "referenced file is not in the package", cleaned))
    return findings


def _supply_chain(package: Path, files: list[Path], frontmatter: dict | None) -> dict:
    """What the package declares about where it came from, and what it reaches for.

    Everything here is advisory and nothing here fails a package. These are the facts a reviewer
    needs in front of them; none of them is a security verdict, and this command does not pretend
    to detect prompt injection -- that is a semantic property no deterministic scan establishes."""
    section = _section()
    declared = frontmatter or {}

    if not declared.get("source"):
        _add(section, Finding("source-metadata-missing", WARNING,
                              "no source declared: the package does not record where it came from"))
    if not declared.get("license"):
        _add(section, Finding("license-metadata-missing", WARNING,
                              "no license declared"))

    executables = [path.relative_to(package).as_posix() for path in files
                   if path.stat().st_mode & 0o111 and not path.is_symlink()]
    for relative in executables:
        _add(section, Finding("executable-asset", WARNING,
                              "asset is executable", relative))

    urls, mutable = set(), set()
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        urls.update(_URL.findall(text))
        mutable.update(_MUTABLE_REF.findall(text))

    # One aggregate finding, not one per URL: a documentation-heavy skill has dozens of links, and
    # a wall of identical warnings is how a reviewer learns to stop reading them. The full list
    # stays in `remote_references` for anything consuming the JSON.
    if urls:
        _add(section, Finding(
            "remote-reference", WARNING,
            f"package references {len(urls)} remote URL(s): {', '.join(sorted(urls)[:3])}"
            + (" …" if len(urls) > 3 else "")))
    if mutable:
        _add(section, Finding(
            "reference-unpinned", WARNING,
            "reference points at a moving branch rather than a commit or tag; what it returns "
            "today is not what it will return later"))

    section.update(source=declared.get("source"), license=declared.get("license"),
                   executables=executables, remote_references=sorted(urls))
    return section


def _collision(package: Path, name: str, library_root: Path | None) -> dict:
    """Deterministic collision only.

    Description shadowing -- the thing that actually steals traffic -- is a cosine comparison that
    needs the embedding router, which needs a model. Approximating it here would be worse than not
    answering, so it reports UNMEASURED and names `ingot.optimize.routing_health`, which already does the
    real library-wide scan."""
    semantic = {"status": UNMEASURED,
                "reason": "description shadowing needs the embedding router",
                "measure_with": "python -m ingot.optimize.routing_health"}

    if library_root is None:
        return _section(UNMEASURED, reason="no library root supplied", semantic=semantic)

    # `skill_sources`, not a bare glob: it skips the dot-prefixed staging directories promotion and
    # rollback leave beside a live skill, each of which carries a complete SKILL.md. Globbing
    # directly would report an abandoned `.pdf.<hex>.stage` as a colliding skill.
    section = _section(semantic=semantic, library_root=str(library_root))
    existing = {path.parent.name for path in skill_sources(library_root)
                if path.parent.resolve() != package.resolve()}
    if name in existing:
        _add(section, Finding(
            "name-collision", WARNING,
            f"the library already has a skill named {name!r}; one would shadow the other"))
    for other in sorted(existing):
        if other != name and other.casefold() == name.casefold():
            _add(section, Finding(
                "name-case-collision", WARNING,
                f"the library has {other!r}, which differs from {name!r} only by case"))
    return section


def _activation(name: str, evidence_root: Path) -> dict:
    """Whether a routing suite exists is a fact available offline. Whether the router loads this
    skill at the right time is not, so it is never reported as a score."""
    suite = evidence_root / "ingot" / "optimize" / "tasks" / f"{name}.yaml"
    cases = 0
    if suite.is_file():
        try:
            loaded = yaml.safe_load(suite.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            loaded = {}
        routing = loaded.get("routing") if isinstance(loaded, dict) else None
        cases = len(routing) if isinstance(routing, list) else 0

    return _section(
        UNMEASURED,
        routing_cases=cases,
        reason=("a routing suite exists but scoring it needs the embedding router"
                if cases else "no routing suite for this skill"),
        measure_with=f"python -m ingot.optimize.routing_health {name}",
    )


def _behavioral(name: str, evidence_root: Path) -> dict:
    """Surface what `ingot.optimize.compat` already measured. Never recompute it: that costs a model, a
    key, and money, and this command promises none of the three."""
    path = evidence_root / "runs" / "compat" / f"{name}.json"
    if not path.is_file():
        return _section(UNMEASURED,
                        reason="no compatibility evidence for this skill",
                        measure_with=f"python -m ingot.optimize.compat {name}")
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        return _section(UNMEASURED, reason=f"compatibility evidence is unreadable: {error}")

    return _section(MEASURED, source=str(path), tasks=summary.get("tasks"),
                    judge=summary.get("judge"), models=summary.get("models") or {})


def review_package(package: Path, *, library_root: Path | None = None,
                   evidence_root: Path | None = None) -> dict:
    """Review one skill package. Reads only; writes nothing, anywhere."""
    package = Path(package)
    evidence_root = Path(evidence_root) if evidence_root is not None \
        else Path(__file__).resolve().parent.parent

    files, escapes = _package_files(package)
    structural, frontmatter = _structural(package, files, escapes)
    name = structural.get("name", package.name)

    try:
        revision = skill_revision(package)
    except (ValueError, OSError) as error:
        revision = ""
        _add(structural, Finding("revision-unavailable", ERROR,
                                 f"cannot compute a content revision: {error}"))

    sections = {
        "structural": structural,
        "supply_chain": _supply_chain(package, files, frontmatter),
        "collision": _collision(package, name, library_root),
        "activation": _activation(name, evidence_root),
        "behavioral": _behavioral(name, evidence_root),
    }

    findings = [finding for section in sections.values() for finding in section["findings"]]
    errors = sum(1 for finding in findings if finding["level"] == ERROR)
    return {
        "schema_version": REVIEW_SCHEMA,
        "package": str(package),
        "skill": name,
        "revision": revision,
        "valid": errors == 0,
        "errors": errors,
        "warnings": sum(1 for finding in findings if finding["level"] == WARNING),
        "sections": sections,
    }


def render(result: dict) -> str:
    """One block per section, so a reader sees which question failed rather than a number."""
    verdict = "VALID" if result["valid"] else "INVALID"
    lines = [f"{result['skill']}  {verdict}  {result['errors']} error(s), "
             f"{result['warnings']} warning(s)",
             f"  package   {result['package']}",
             f"  revision  {result['revision'][:16] or '(unavailable)'}"]

    for title, section in result["sections"].items():
        status = "" if section["status"] == MEASURED else f"  [{section['status'].upper()}]"
        lines.append(f"\n{title.replace('_', ' ')}{status}")
        if section["status"] == UNMEASURED and section.get("reason"):
            lines.append(f"  {section['reason']}")
            if section.get("measure_with"):
                lines.append(f"  measure with: {section['measure_with']}")
        for finding in section["findings"]:
            where = f" ({finding['path']})" if finding.get("path") else ""
            lines.append(f"  {finding['level']:<7} {finding['code']}: {finding['message']}{where}")
        if not section["findings"] and section["status"] == MEASURED:
            lines.append("  no findings")
    return "\n".join(lines)
