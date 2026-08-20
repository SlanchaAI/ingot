"""Discover skills from skills/<name>/SKILL.md. The YAML frontmatter `description` is the routing
key; the markdown body is what an agent loads. No compilation, no DB, a skill is just its SKILL.md."""
from __future__ import annotations
import hashlib
import json
import os
import re
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from pathlib import PurePosixPath
from typing import Iterable

import yaml
from ingot import paths

def library_dir() -> Path:
    """The local authoring root. A function, not a constant: it is configuration, and a value
    frozen at import cannot follow a process that is told where its state lives."""
    return paths.library()


_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

# One slug rule for every layer (promotion, UI), a name one layer accepts
# must be accepted by all of them, or a skill becomes un-promotable.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


_ROUTER_DEFAULTS = {
    "harnesses": ["claude", "codex"],
    "scopes": ["global"],
    "path_patterns": [],
    "required_tools": [],
    "required_mcps": [],
    "trust": "unknown",
    "activation": "automatic",
    "platforms": ["macos", "linux", "windows"],
    "priority": 50,
    "conflicts": [],
}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: str
    revision: str = ""
    root: str = ""
    metadata: dict = field(default_factory=dict)
    variants: dict[str, str] = field(default_factory=dict)

    def body_for(self, harness: str) -> str:
        return self.variants.get(harness, self.body)


def parse_skill(md: str, fallback_name: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body). The dict always has `name` and `description` keys; other
    fields (license, source, …) are preserved so writers can round-trip them."""
    m = _FRONTMATTER.match(md)
    if not m:
        return {"name": fallback_name, "description": ""}, md.strip()
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta["name"] = str(meta.get("name") or fallback_name)
    meta["description"] = str(meta.get("description") or "").strip()
    return meta, m.group(2).strip()


def skill_sources(library_root: Path) -> list[Path]:
    """Every SKILL.md a library root publishes, in a stable order.

    Dot-prefixed directories are skipped: promotion and rollback stage a skill beside the live one
    as `.<name>.<hex>.stage` / `.previous` / `.rollback`, and those carry a complete SKILL.md.
    `Path.glob` matches hidden names (unlike a shell), and '.' sorts ahead of every slug, so an
    abandoned staging directory would otherwise win the duplicate-name check and shadow the live
    skill. Nothing legitimate publishes a skill from a hidden directory."""
    if not library_root.exists():
        return []
    return sorted(path for path in library_root.glob("*/SKILL.md")
                  if not path.parent.name.startswith("."))


def configured_roots(explicit: Iterable[str | Path] | None = None) -> list[Path]:
    """Canonical skill-library roots. Explicit CLI roots replace the environment configuration."""
    values = list(explicit) if explicit is not None else [
        p for p in os.environ.get("SKILL_ROUTER_PATHS", "").split(os.pathsep) if p
    ]
    values = [library_dir(), *values] if values else [library_dir()]
    roots: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        root = Path(value).expanduser().resolve()
        if root not in seen:
            roots.append(root)
            seen.add(root)
    return roots


def _router_metadata(meta: dict) -> dict:
    raw = meta.get("metadata") or {}
    extension = raw.get("skill-router") if isinstance(raw, dict) else {}
    extension = extension if isinstance(extension, dict) else {}
    result = {key: list(value) if isinstance(value, list) else value
              for key, value in _ROUTER_DEFAULTS.items()}
    for key in result:
        if key in extension:
            result[key] = extension[key]
    list_fields = ("harnesses", "scopes", "path_patterns", "required_tools", "required_mcps",
                   "platforms", "conflicts")
    for key in list_fields:
        if not isinstance(result[key], list) or not all(isinstance(item, str) for item in result[key]):
            raise ValueError(f"metadata.skill-router.{key} must be a list of strings")
    for pattern in result["path_patterns"]:
        path = PurePosixPath(pattern)
        if (not pattern or path.is_absolute() or ".." in path.parts or "\\" in pattern or
                any(ord(char) < 32 for char in pattern)):
            raise ValueError("metadata.skill-router.path_patterns must be relative POSIX globs")
    try:
        result["priority"] = int(result["priority"])
    except (TypeError, ValueError):
        result["priority"] = 50
    return result


def _contained_file(skill_root: Path, path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(skill_root)
    except ValueError as exc:
        raise ValueError(f"skill file escapes skill root: {path}") from exc
    return resolved


def skill_revision(skill_root: Path, components: dict[str, str] | None = None) -> str:
    """Hash the complete logical skill, optionally with prospective component replacements."""
    skill_root = skill_root.resolve()
    files: dict[str, Path] = {}
    for path in skill_root.rglob("*"):
        if path.is_symlink():
            _contained_file(skill_root, path)
        if path.is_file():
            safe = _contained_file(skill_root, path)
            files[path.relative_to(skill_root).as_posix()] = safe
    for key in (components or {}):
        if not key.startswith("file:"):
            continue
        relative = Path(key[len("file:"):])
        if relative.as_posix() == "SKILL.md" or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"component escapes skill root: {relative}")
        _contained_file(skill_root, skill_root / relative)
        files.setdefault(relative.as_posix(), skill_root / relative)
    # A quarantined creation has no directory or SKILL.md yet. Include its logical SKILL.md in
    # the same digest shape an activated skill will use, so approval can bind the exact candidate
    # before any filesystem mutation occurs.
    if components is not None and "SKILL.md" not in files:
        files["SKILL.md"] = skill_root / "SKILL.md"

    digest = hashlib.sha256()
    for relative, path in sorted(files.items()):
        digest.update(relative.encode())
        digest.update(b"\0")
        if relative == "SKILL.md":
            if path.exists():
                meta, body = parse_skill(
                    path.read_text(encoding="utf-8", errors="ignore"), skill_root.name)
            else:
                meta, body = {"name": skill_root.name, "description": ""}, ""
            if components is not None:
                if "frontmatter" in components:
                    try:
                        supplied = json.loads(components["frontmatter"])
                    except (TypeError, ValueError) as exc:
                        raise ValueError("frontmatter component is not valid JSON") from exc
                    meta = normalized_frontmatter(skill_root.name,
                                                  components["description"], supplied)
                else:
                    meta["description"] = components["description"]
                body = components["body"]
            digest.update(yaml.safe_dump(meta, sort_keys=True, allow_unicode=True).encode())
            digest.update(b"\0")
            digest.update(body.strip().encode())
        else:
            replacement = (components or {}).get(f"file:{relative}")
            digest.update(replacement.encode() if replacement is not None else path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_skills(skills_dir: Path | None = None, *, roots: Iterable[str | Path] | None = None) -> list[Skill]:
    """Load skills in declared root order; first duplicate identity wins with a visible warning."""
    selected_roots = configured_roots(roots if roots is not None else ([skills_dir] if skills_dir else None))
    skills: list[Skill] = []
    by_name: dict[str, Path] = {}
    for library_root in selected_roots:
        for source_path in skill_sources(library_root):
            skill_root = source_path.parent.resolve()
            sk = _contained_file(skill_root, source_path)
            meta, body = parse_skill(sk.read_text(encoding="utf-8", errors="ignore"), skill_root.name)
            if not meta["description"]:
                continue
            name = meta["name"]
            if name in by_name:
                warnings.warn(f"duplicate skill '{name}': keeping {by_name[name]}, skipping {sk}",
                              UserWarning, stacklevel=2)
                continue
            variants: dict[str, str] = {}
            variants_dir = skill_root / "variants"
            for harness in ("claude", "codex"):
                variant = variants_dir / f"{harness}.md"
                if variant.exists():
                    safe_variant = _contained_file(skill_root, variant)
                    variants[harness] = safe_variant.read_text(encoding="utf-8", errors="ignore").strip()
            by_name[name] = sk
            skills.append(Skill(
                name=name,
                description=meta["description"],
                body=body,
                path=str(sk),
                revision=skill_revision(skill_root),
                root=str(skill_root),
                metadata=_router_metadata(meta),
                variants=variants,
            ))
    return skills


def resolve_skill_dir(name: str) -> Path:
    """The directory `name` actually lives in, across every configured root.

    `library_dir() / name` is only correct for the one *writable* authoring root. A merged library
    serves most of its skills from read-only mounts, so that path finds them by luck or not at
    all — and every optimize entry point used to build it by hand, which meant the optimizer
    silently refused to touch anything it did not itself author."""
    for item in load_skills():
        if item.name == name:
            return Path(item.root)
    raise LookupError(f"no indexed skill named '{name}'; check SKILL_ROUTER_PATHS")


def writable_skill_dir(name: str) -> Path:
    """The activation destination in the first-precedence writable authoring root.

    This is not a lookup: callers must still use ``resolve_skill_dir`` to read the serving
    revision. Promotion uses this destination only after resolving and validating that revision,
    because merged libraries may supply it from a read-only mount.
    """
    if not SLUG_RE.fullmatch(name):
        raise ValueError(f"invalid skill name: {name!r}")
    return library_dir().expanduser().resolve() / name


# --- writing / full-skill components (used by candidate generation and promotion) ---

_TEXT_SUFFIXES = {".md", ".txt", ".py", ".sh", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".cfg"}


def write_skill_md(path: Path, meta: dict, body: str) -> None:
    """The one place SKILL.md is serialized. yaml.safe_dump quotes/escapes every field so a stray
    '---' or 'name:' in model output can't corrupt the frontmatter. `meta` carries all frontmatter
    fields (name, description, license, source, …) so they round-trip."""
    meta = dict(meta)
    meta["description"] = " ".join(str(meta.get("description", "")).split())
    dumped = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=100000)
    path.write_text(f"---\n{dumped}---\n\n{body.strip()}\n", encoding="utf-8")


def normalized_frontmatter(name: str, description: str, frontmatter: dict | None = None) -> dict:
    """Canonical, router-valid metadata for a proposed skill before revision hashing.

    Creation cannot hash caller bytes and normalize them only during activation: that would let
    the reviewed revision differ from what routing serves. A safe YAML round trip also rejects
    object types that could not be persisted as portable frontmatter.
    """
    if frontmatter is not None and not isinstance(frontmatter, dict):
        raise ValueError("frontmatter must be an object")
    try:
        meta = yaml.safe_load(yaml.safe_dump(frontmatter or {}, allow_unicode=True)) or {}
    except yaml.YAMLError as exc:
        raise ValueError("frontmatter must contain YAML-safe values") from exc
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be an object")
    meta["name"] = name
    meta["description"] = " ".join(str(description).split())
    _router_metadata(meta)
    return meta


def read_components(skill_dir: Path) -> dict[str, str]:
    """Every optimizable text component of a skill: its routing `description`, its SKILL.md `body`,
    and each bundled text file as `file:<relpath>`. This is the unit a candidate rewrite works on."""
    skill_root = skill_dir.resolve()
    md_path = _contained_file(skill_root, skill_dir / "SKILL.md")
    meta, body = parse_skill(md_path.read_text(encoding="utf-8", errors="ignore"), skill_dir.name)
    comps = {"description": meta["description"], "body": body}
    for f in sorted(skill_dir.rglob("*")):
        if f.is_file() and f.name != "SKILL.md" and f.suffix.lower() in _TEXT_SUFFIXES:
            safe = _contained_file(skill_root, f)
            relative = f.relative_to(skill_dir).as_posix()
            comps[f"file:{relative}"] = safe.read_text(encoding="utf-8", errors="ignore")
    return comps


def optimizable_components(skill_dir: Path) -> dict[str, str]:
    """The components a candidate may rewrite: just the routing `description` and the SKILL.md
    `body`, which is what the agent actually loads and what the A/B measures. Bundled files
    (reference docs, scripts, LICENSE) are deliberately excluded: they aren't served or executed in
    the A/B, and a text rewriter has no business touching a license or unrun code. write_components
    leaves them untouched on disk, so they're preserved across a promotion."""
    c = read_components(skill_dir)
    return {"description": c["description"], "body": c["body"]}


def write_components(skill_dir: Path, comps: dict[str, str]) -> None:
    """Write a full skill back, preserving existing frontmatter (name, license, source, …) and only
    updating the `description`, `body`, and each bundled `file:<relpath>` component."""
    skill_root = skill_dir.resolve()
    md_path = _contained_file(skill_root, skill_dir / "SKILL.md")
    component_paths: dict[str, Path] = {}
    for key in comps:
        if not key.startswith("file:"):
            continue
        relative = Path(key[len("file:"):])
        if relative.as_posix() == "SKILL.md" or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"component escapes skill root: {relative}")
        component_paths[key] = _contained_file(skill_root, skill_dir / relative)

    meta, _ = parse_skill(md_path.read_text(encoding="utf-8", errors="ignore"), skill_dir.name)
    meta["description"] = comps["description"]
    md_tmp = md_path.with_name(f".{md_path.name}.{uuid.uuid4().hex}.tmp")
    write_skill_md(md_tmp, meta, comps["body"])
    md_tmp.replace(md_path)
    for key, p in component_paths.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(comps[key], encoding="utf-8")
        tmp.replace(p)
