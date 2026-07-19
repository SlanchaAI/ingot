"""Gate-enforced, revisioned, atomic promotion for quarantined skill challengers."""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from mcp_server import guard_model, safety
from mcp_server.registry import (SKILLS_DIR, SLUG_RE, load_skills, name_problem, skill_revision,
                                 write_components, write_skill_md)
from mcp_server.router import Router

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
PENDING_DIR = RUNS_DIR / "pending"
REVISIONS_DIR = RUNS_DIR / "revisions"
COLLISION_SCORE = float(os.environ.get("COLLISION_SCORE", "0.93"))


def check_slug(skill: str) -> str:
    if not SLUG_RE.fullmatch(skill):
        raise ValueError(f"invalid skill name: {skill!r}")
    return skill


def pending_path(skill: str) -> Path:
    return PENDING_DIR / f"{check_slug(skill)}.json"


def save_pending(skill: str, data: dict) -> Path:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    path = pending_path(skill)
    _archive_displaced(skill, path, data)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)
    return path


def _archive_displaced(skill: str, path: Path, data: dict) -> None:
    """Each skill has ONE review slot. A new challenger from a DIFFERENT pass (other changed
    components) must not silently destroy a reviewable one, so the displaced challenger is
    archived beside the slot; re-runs of the same pass overwrite in place as before."""
    if not path.exists():
        return
    existing = json.loads(path.read_text())
    if sorted(existing.get("changed_components", [])) == sorted(data.get("changed_components", [])):
        return
    archived = PENDING_DIR / f"{skill}.displaced-{existing.get('created', uuid.uuid4().hex)}.json"
    shutil.copy(path, archived)
    print(f"[pending] one review slot per skill: the pending "
          f"{existing.get('changed_components')} challenger was displaced by this "
          f"{data.get('changed_components')} challenger and archived to {archived} "
          f"(promote or reject before running a different pass to avoid this)")


def load_pending(skill: str) -> dict | None:
    path = pending_path(skill)
    return json.loads(path.read_text()) if path.exists() else None


def list_pending() -> list[dict]:
    """Return valid pending records without letting a malformed queue file break the review UI."""
    if not PENDING_DIR.exists():
        return []
    records = []
    for path in sorted(PENDING_DIR.glob("*.json")):
        if not SLUG_RE.fullmatch(path.stem):
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("skill") == path.stem:
            records.append(record)
    return records


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


def _audit(action: str, skill: str, revision: str, actor: str = "local-operator") -> None:
    """Append a minimal approval trail without recording skill bodies or credentials."""
    import time
    audit_root = PENDING_DIR.parent
    audit_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(audit_root, 0o700)
    audit_file = audit_root / "approval-audit.jsonl"
    fd = os.open(audit_file, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        record = {"schema_version": 1, "ts": int(time.time()), "action": action,
                  "skill": skill, "revision": revision, "actor": actor}
        os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode())
    finally:
        os.close(fd)


def _require_promotable(pending: dict) -> None:
    gate = pending.get("gate", {})
    if gate.get("promotable") is not True:
        reasons = "; ".join(gate.get("blocked", [])) or "unspecified failure"
        raise ValueError(f"Behavioral CI gate blocked promotion: {reasons}")


def _creation_components(pending: dict) -> tuple[str, str]:
    components = pending.get("challenger_components", {})
    description = components.get("description")
    body = components.get("body")
    if not isinstance(description, str):
        raise ValueError("pending creation has invalid skill description")
    if not isinstance(body, str):
        raise ValueError("pending creation has invalid skill body")
    return description, body


def _validate_creation_name(skill: str, pending: dict) -> None:
    if pending.get("skill") != skill:
        raise ValueError("pending candidate identity does not match requested skill")
    problem = name_problem(skill)
    if problem:
        raise ValueError(f"invalid skill name '{skill}': {problem}")


def _validate_creation_available(skill: str, active: list) -> None:
    if (SKILLS_DIR / skill).exists():
        raise ValueError(f"skill '{skill}' already exists")
    active_names = {item.name for item in active}
    if skill in active_names:
        raise ValueError(f"skill '{skill}' already exists")


def _validate_creation_content(skill: str, description: str, body: str) -> None:
    problems = safety.scan(description, body)
    ml_flag = guard_model.check(f"{description}\n{body}")
    if ml_flag:
        problems.append(ml_flag)
    if problems:
        raise ValueError(f"skill '{skill}' rejected: {'; '.join(problems)}")


def _validate_creation_collision(skill: str, description: str, active: list) -> None:
    if not active:
        return
    shadowed, score = Router(active).nearest(description)
    if score >= COLLISION_SCORE:
        raise ValueError(
            f"skill '{skill}' rejected: description too similar to existing skill "
            f"'{shadowed}' (cosine {score:.2f})"
        )


def _validated_creation(skill: str, pending: dict) -> tuple[str, str]:
    active = load_skills()
    description, body = _creation_components(pending)
    _validate_creation_name(skill, pending)
    _validate_creation_available(skill, active)
    _validate_creation_content(skill, description, body)
    _validate_creation_collision(skill, description, active)
    return description, body


def _activate_creation(skill: str, pending: dict) -> str:
    description, body = _validated_creation(skill, pending)
    destination = SKILLS_DIR / skill

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    stage = destination.with_name(f".{skill}.{uuid.uuid4().hex}.stage")
    stage.mkdir()
    try:
        write_skill_md(stage / "SKILL.md", {
            "name": skill,
            "description": description,
            "source": pending.get("source", "agent"),
        }, body)
        if destination.exists():
            raise ValueError(f"skill '{skill}' already exists")
        stage.rename(destination)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    pending_path(skill).unlink(missing_ok=True)
    return f"Activated new skill '{skill}' after human approval."


def _activate_rewrite(skill: str, pending: dict) -> str:
    components = pending["challenger_components"]
    evidence = pending.get("evidence")
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


def approve_pending(skill: str, actor: str = "local-operator") -> str:
    """Activate one pending creation or tested rewrite after an explicit approval action."""
    skill = check_slug(skill)
    pending = load_pending(skill)
    if not pending:
        raise ValueError(f"no pending challenger for '{skill}'")
    _require_promotable(pending)
    if pending.get("kind") == "creation":
        result = _activate_creation(skill, pending)
        revision = skill_revision(SKILLS_DIR / skill)
    else:
        result = _activate_rewrite(skill, pending)
        revision = _current_skill(skill).revision
    _audit("approve", skill, revision, actor)
    return result


def rollback(skill: str, revision: str, actor: str = "local-operator") -> str:
    """Atomically restore a snapshot while preserving the displaced current revision."""
    skill = check_slug(skill)
    if not SLUG_RE.fullmatch(revision):
        raise ValueError(f"invalid revision: {revision!r}")
    current = _current_skill(skill)
    source = REVISIONS_DIR / skill / revision
    if not source.is_dir():
        raise ValueError(f"no snapshot for '{skill}' at revision {revision}")
    skill_dir = Path(current.root)
    _snapshot(skill_dir, skill, current.revision)
    stage = skill_dir.with_name(f".{skill_dir.name}.{uuid.uuid4().hex}.rollback")
    previous = skill_dir.with_name(f".{skill_dir.name}.{uuid.uuid4().hex}.previous")
    shutil.copytree(source, stage, symlinks=True)
    try:
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
    restored = _current_skill(skill)
    _audit("rollback", skill, restored.revision, actor)
    return f"Rolled back '{skill}' from {current.revision} to {restored.revision}."


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Approve or roll back a revisioned skill")
    sub = parser.add_subparsers(dest="command", required=True)
    approve_parser = sub.add_parser("approve")
    approve_parser.add_argument("skill")
    rollback_parser = sub.add_parser("rollback")
    rollback_parser.add_argument("skill")
    rollback_parser.add_argument("revision")
    args = parser.parse_args()
    print(approve_pending(args.skill) if args.command == "approve"
          else rollback(args.skill, args.revision))
