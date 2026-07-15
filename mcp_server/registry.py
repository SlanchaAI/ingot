"""Discover skills from skills/<name>/SKILL.md. The YAML frontmatter `description` is the routing
key; the markdown body is what an agent loads. No compilation, no DB — a skill is just its SKILL.md."""
from __future__ import annotations
import hashlib
import os
import re
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

# One slug rule for every layer (create_skill, promotion, UI) — a name one layer accepts
# must be accepted by all of them, or a skill becomes un-promotable.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Anthropic Agent Skills frontmatter rules for `name`: ≤64 chars, no reserved words.
MAX_NAME = 64
_RESERVED = ("anthropic", "claude")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())
    return re.sub(r"-+", "-", s).strip("-")


def name_problem(slug: str) -> str | None:
    """Reason a skill name is invalid per the Agent Skills spec, or None if it's fine."""
    if not SLUG_RE.fullmatch(slug):
        return "name must be a slug: lowercase letters, digits, and hyphens"
    if len(slug) > MAX_NAME:
        return f"name exceeds {MAX_NAME} characters"
    if any(r in slug for r in _RESERVED):
        return f"name contains a reserved word ({', '.join(_RESERVED)})"
    return None


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


def configured_roots(explicit: Iterable[str | Path] | None = None) -> list[Path]:
    """Canonical skill-library roots. Explicit CLI roots replace the environment configuration."""
    values = list(explicit) if explicit is not None else [
        p for p in os.environ.get("SKILL_ROUTER_PATHS", "").split(os.pathsep) if p
    ]
    values = [SKILLS_DIR, *values] if values else [SKILLS_DIR]
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

    digest = hashlib.sha256()
    for relative, path in sorted(files.items()):
        digest.update(relative.encode())
        digest.update(b"\0")
        if relative == "SKILL.md":
            meta, body = parse_skill(path.read_text(encoding="utf-8", errors="ignore"), skill_root.name)
            if components is not None:
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
        if not library_root.exists():
            continue
        for source_path in sorted(library_root.glob("*/SKILL.md")):
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


# --- writing / full-skill components (used by create_skill, the optimizer, and promotion) ---

_TEXT_SUFFIXES = {".md", ".txt", ".py", ".sh", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".cfg"}


def write_skill_md(path: Path, meta: dict, body: str) -> None:
    """The one place SKILL.md is serialized. yaml.safe_dump quotes/escapes every field so a stray
    '---' or 'name:' in model output can't corrupt the frontmatter. `meta` carries all frontmatter
    fields (name, description, license, source, …) so they round-trip."""
    meta = dict(meta)
    meta["description"] = " ".join(str(meta.get("description", "")).split())
    dumped = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=100000)
    path.write_text(f"---\n{dumped}---\n\n{body.strip()}\n", encoding="utf-8")


def read_components(skill_dir: Path) -> dict[str, str]:
    """Every optimizable text component of a skill: its routing `description`, its SKILL.md `body`,
    and each bundled text file as `file:<relpath>`. This is the unit GEPA evolves for a full skill."""
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
    """The components GEPA may rewrite: just the routing `description` and the SKILL.md `body` — what
    the agent actually loads and what the A/B measures. Bundled files (reference docs, scripts,
    LICENSE) are deliberately excluded: they aren't served/executed in the A/B, and a text optimizer
    has no business touching a license or unrun code. write_components leaves them untouched on disk,
    so they're preserved across a promotion."""
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
