"""Gate-enforced, revisioned, atomic promotion for quarantined skill challengers."""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from mcp_server.registry import SLUG_RE, load_skills, skill_revision, write_components

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
PENDING_DIR = RUNS_DIR / "pending"
REVISIONS_DIR = RUNS_DIR / "revisions"


def check_slug(skill: str) -> str:
    if not SLUG_RE.fullmatch(skill):
        raise ValueError(f"invalid skill name: {skill!r}")
    return skill


def pending_path(skill: str) -> Path:
    return PENDING_DIR / f"{check_slug(skill)}.json"


def save_pending(skill: str, data: dict) -> Path:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    path = pending_path(skill)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)
    return path


def load_pending(skill: str) -> dict | None:
    path = pending_path(skill)
    return json.loads(path.read_text()) if path.exists() else None


def _current_skill(skill: str):
    matches = [item for item in load_skills() if item.name == skill]
    if not matches:
        raise ValueError(f"no indexed skill named '{skill}'")
    return matches[0]


def _validate_evidence(current, components: dict[str, str], evidence: dict) -> None:
    gate = evidence.get("gate", {})
    if gate.get("promotable") is not True:
        reasons = "; ".join(gate.get("blocked", [])) or "unspecified failure"
        raise ValueError(f"Behavioral CI gate blocked promotion: {reasons}")
    if evidence.get("champion", {}).get("revision") != current.revision:
        raise ValueError("champion revision changed since Behavioral CI; rerun improve")
    expected = evidence.get("challenger", {}).get("revision")
    if expected != skill_revision(Path(current.root), components):
        raise ValueError("challenger revision does not match Behavioral CI evidence")


def _snapshot(skill_dir: Path, skill: str, revision: str) -> Path:
    destination = REVISIONS_DIR / skill / revision
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{revision}.{uuid.uuid4().hex}.tmp")
        shutil.copytree(skill_dir, temporary, symlinks=True)
        temporary.rename(destination)
    return destination


def promote(skill: str, components: dict[str, str] | None = None,
            evidence: dict | None = None) -> str:
    """Promote only a tested challenger; snapshot, stage, swap, and roll back on failure."""
    skill = check_slug(skill)
    pending = load_pending(skill)
    if components is None:
        if not pending:
            raise ValueError(f"no pending challenger for '{skill}'")
        components = pending["challenger_components"]
    evidence = evidence or (pending or {}).get("evidence")
    if not evidence:
        raise ValueError("Behavioral CI evidence is required for promotion")

    current = _current_skill(skill)
    _validate_evidence(current, components, evidence)
    skill_dir = Path(current.root)
    _snapshot(skill_dir, skill, current.revision)

    stage = skill_dir.with_name(f".{skill_dir.name}.{uuid.uuid4().hex}.stage")
    previous = skill_dir.with_name(f".{skill_dir.name}.{uuid.uuid4().hex}.previous")
    shutil.copytree(skill_dir, stage, symlinks=True)
    try:
        write_components(stage, components)
        skill_dir.rename(previous)
        try:
            stage.rename(skill_dir)
        except BaseException:
            previous.rename(skill_dir)
            raise
        shutil.rmtree(previous, ignore_errors=True)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    pending_path(skill).unlink(missing_ok=True)
    return f"Promoted '{skill}' from revision {current.revision}; previous revision snapshotted."
