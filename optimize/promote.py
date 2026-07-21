"""Gate-enforced, revisioned, atomic promotion and rollback for quarantined skill changes."""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from mcp_server.registry import SLUG_RE, load_skills, skill_revision, write_components

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
PENDING_DIR = RUNS_DIR / "pending"
REVISIONS_DIR = RUNS_DIR / "revisions"
logger = logging.getLogger(__name__)


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


def snapshot_index_path(skill: str) -> Path:
    """When each snapshot was last taken. It lives beside the snapshot directories, never inside
    one, so a rollback copies back the skill and nothing else. The leading dot also keeps it out of
    the slug-matched snapshot listing."""
    return REVISIONS_DIR / check_slug(skill) / ".snapshots.json"


def _read_snapshot_index(skill: str) -> dict:
    """Tolerate an unreadable or hand-edited index: ordering degrades, history still renders."""
    try:
        raw = snapshot_index_path(skill).read_text(encoding="utf-8", errors="replace")
        index = json.loads(raw)
    except (OSError, ValueError):
        return {}
    return index if isinstance(index, dict) else {}


def _index_number(value: object, fallback: int) -> int:
    """One ordering key from a hand-editable index, or the caller's fallback.

    The index is plain JSON an operator can edit, so an entry can hold a string, a list, or null
    where a number belongs. Tolerating corruption has to mean falling back, not raising: a
    `TypeError` from comparing a string against an int would break the very listing and stamping
    that the fallback exists to keep working. `bool` is excluded because `True` is not a sequence
    number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return int(value)


def _sequence_numbers(index: dict) -> list[int]:
    return [_index_number(entry.get("seq"), 0) for entry in index.values() if isinstance(entry, dict)]


def _stamp_snapshot(skill: str, revision: str) -> None:
    """Record that this revision was snapshotted now.

    Directory mtime cannot order rollback targets: `copytree` copies the source directory's
    timestamps onto the snapshot, and re-snapshotting an existing revision (rollback, then promote
    away from the restored revision again) copies nothing at all. The sequence recorded here is
    what makes 'most recently snapshotted' true in both cases."""
    index = _read_snapshot_index(skill)
    highest = max(_sequence_numbers(index), default=0)
    index[revision] = {"created": int(time.time()), "seq": highest + 1}
    path = snapshot_index_path(skill)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".snapshots.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _stamp_snapshot_best_effort(skill: str, revision: str) -> None:
    """A snapshot that cannot be stamped is still a valid rollback target: keep the promotion.

    Every failure is caught, not just the unwritable-store one: the index is hand-editable JSON,
    and a promotion must not be lost to whatever a corrupt entry makes the stamping code raise."""
    try:
        _stamp_snapshot(skill, revision)
    except Exception:
        logger.warning("Snapshotted %r at revision %s, but the snapshot index write failed",
                       skill, revision, exc_info=True)


def list_revisions(skill: str) -> list[dict]:
    """Snapshot revisions available as rollback targets, most recently snapshotted first. Each
    entry is `{"revision": <hash>, "created": <unix seconds>}`. Snapshots taken before the index
    existed, and snapshots whose index entry is unusable, fall back to directory mtime and sort
    below stamped ones."""
    root = REVISIONS_DIR / check_slug(skill)
    if not root.is_dir():
        return []
    index = _read_snapshot_index(skill)
    records = []
    for path in root.iterdir():
        if not path.is_dir() or not SLUG_RE.fullmatch(path.name):
            continue
        entry = index.get(path.name)
        entry = entry if isinstance(entry, dict) else {}
        records.append({"revision": path.name,
                        "created": (_index_number(entry.get("created"), 0)
                                    or int(path.stat().st_mtime)),
                        "seq": _index_number(entry.get("seq"), 0)})
    records.sort(key=lambda r: (r["seq"], r["created"], r["revision"]), reverse=True)
    return [{"revision": r["revision"], "created": r["created"]} for r in records]


def list_snapshotted_skills() -> list[str]:
    """Skills with at least one snapshot. Reading the snapshot store directly keeps the history
    view off the skill-library hash scan that the skills listing already pays for."""
    if not REVISIONS_DIR.is_dir():
        return []
    return sorted(path.name for path in REVISIONS_DIR.iterdir()
                  if path.is_dir() and SLUG_RE.fullmatch(path.name))


def _current_skill(skill: str):
    matches = [item for item in load_skills() if item.name == skill]
    if not matches:
        raise ValueError(f"no indexed skill named '{skill}'")
    return matches[0]


def _revision_problem(current, components: dict[str, str], evidence: dict) -> str | None:
    """Whether the recorded evidence still describes what is on disk, or why it does not."""
    if evidence.get("champion", {}).get("revision") != current.revision:
        return "champion revision changed since the evidence gate ran; rerun the candidate pass"
    expected = evidence.get("challenger", {}).get("revision")
    if expected != skill_revision(Path(current.root), components):
        return "challenger revision does not match the recorded evidence"
    return None


def _validate_evidence(current, components: dict[str, str], evidence: dict) -> None:
    gate = evidence.get("gate", {})
    if gate.get("promotable") is not True:
        reasons = "; ".join(gate.get("blocked", [])) or "unspecified failure"
        raise ValueError(f"evidence gate blocked promotion: {reasons}")
    problem = _revision_problem(current, components, evidence)
    if problem:
        raise ValueError(problem)


def stale_evidence_reason(skill: str, pending: dict) -> str | None:
    """Why approving this review slot would be refused as stale, or None if it is still fresh.

    The review surface asks before it offers an Approve button, so a card whose champion moved on
    disk (an edited skill, a promotion elsewhere) is blocked at review time rather than at the end
    of an approval click. Gate verdicts are reported separately and are not repeated here."""
    evidence = pending.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        return "evidence is required for promotion"
    try:
        current = _current_skill(skill)
    except ValueError as exc:
        return str(exc)
    return _revision_problem(current, pending.get("challenger_components", {}), evidence)


def _snapshot(skill_dir: Path, skill: str, revision: str) -> Path:
    """Preserve a revision as a rollback target. Re-snapshotting a revision that is already stored
    is a no-op on disk but still restamps it: that is what a rollback followed by a promotion does,
    and the restored revision is then the most recent thing a promotion displaced."""
    destination = REVISIONS_DIR / skill / revision
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{revision}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copytree(skill_dir, temporary, symlinks=True)
            temporary.rename(destination)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    _stamp_snapshot_best_effort(skill, revision)
    return destination


def audit_path() -> Path:
    """The approval trail lives beside the review queue, so a relocated queue moves both."""
    return PENDING_DIR.parent / "approval-audit.jsonl"


def read_audit(limit: int = 50) -> dict:
    """The most recent approval trail records (newest first) and the true total, from one read.

    Unreadable, non-UTF-8, or malformed lines are skipped rather than raised: a trail an operator
    edited by hand, or a partially written record, must not break the review surface. `total`
    counts every record that survives that filter, so a capped page never reads as a total."""
    try:
        text = audit_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"records": [], "total": 0}
    records, total = [], 0
    for line in reversed(text.splitlines()):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        total += 1
        if len(records) < limit:
            records.append(record)
    return {"records": records, "total": total}


def _write_all(fd: int, data: bytes) -> None:
    """os.write may write fewer bytes than asked. Finish the record, so a reader never has to
    parse a half-written audit line."""
    written = 0
    while written < len(data):
        written += os.write(fd, data[written:])


def _audit(action: str, skill: str, revision: str, actor: str = "local-operator") -> None:
    """Append a minimal approval trail without recording skill bodies or credentials.

    `actor` is `local-operator` for every UI action: the local UI has no identity or
    authentication, so the trail records that a local operator approved, not who."""
    audit_file = audit_path()
    audit_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(audit_file.parent, 0o700)
    fd = os.open(audit_file, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        record = {"schema_version": 1, "ts": int(time.time()), "action": action,
                  "skill": skill, "revision": revision, "actor": actor}
        _write_all(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode())
    finally:
        os.close(fd)


def _audit_best_effort(action: str, skill: str, revision: str, actor: str) -> None:
    """Record a committed transition without changing its successful outcome."""
    try:
        _audit(action, skill, revision, actor)
    except Exception:
        logger.warning(
            "Committed %s for skill %r at revision %s, but the audit write failed",
            action, skill, revision, exc_info=True,
        )


def _require_promotable(pending: dict) -> None:
    gate = pending.get("gate", {})
    if gate.get("promotable") is not True:
        reasons = "; ".join(gate.get("blocked", [])) or "unspecified failure"
        raise ValueError(f"evidence gate blocked promotion: {reasons}")


_COPY_SUFFIXES = (".stage", ".rollback")


def _staging_dirs(skill_dir: Path, suffixes: tuple[str, ...]) -> list[Path]:
    """Staging directories this module creates beside a live skill. Matching is a literal prefix
    and suffix rather than a glob, so a directory name is never read as a pattern, and symlinks are
    excluded so a planted link cannot redirect the removal onto its target."""
    prefix = f".{skill_dir.name}."
    try:
        siblings = list(skill_dir.parent.iterdir())
    except OSError:
        return []
    return [path for path in siblings
            if path.name.startswith(prefix) and path.name.endswith(suffixes)
            and path.is_dir() and not path.is_symlink()]


def _sweep_staging(skill_dir: Path) -> None:
    """Remove staging directories a killed or failed run left behind, before staging a new one.

    `.stage` and `.rollback` are always copies of something that still exists (the live skill plus
    the pending record, or a stored snapshot), so they are always discardable. `.previous` holds
    the displaced live directory, and is only discardable once the live directory is back: a run
    killed mid-swap leaves the skill's only copy there. This assumes the one-logical-writer-per-
    skill model the rest of the store assumes (see ARCHITECTURE, Stores and ownership)."""
    suffixes = _COPY_SUFFIXES + ((".previous",) if skill_dir.is_dir() else ())
    for stale in _staging_dirs(skill_dir, suffixes):
        try:
            shutil.rmtree(stale)
        except OSError:
            logger.warning("Could not remove the stale staging directory %s", stale, exc_info=True)


def _activate_rewrite(skill: str, pending: dict) -> str:
    components = pending["challenger_components"]
    evidence = pending.get("evidence")
    if not evidence:
        raise ValueError("evidence is required for promotion")

    current = _current_skill(skill)
    _validate_evidence(current, components, evidence)
    skill_dir = Path(current.root)
    _snapshot(skill_dir, skill, current.revision)
    _sweep_staging(skill_dir)

    stage = skill_dir.with_name(f".{skill_dir.name}.{uuid.uuid4().hex}.stage")
    previous = skill_dir.with_name(f".{skill_dir.name}.{uuid.uuid4().hex}.previous")
    try:
        shutil.copytree(skill_dir, stage, symlinks=True)
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
    """Activate one tested rewrite after an explicit approval action."""
    skill = check_slug(skill)
    pending = load_pending(skill)
    if not pending:
        raise ValueError(f"no pending challenger for '{skill}'")
    _require_promotable(pending)
    result = _activate_rewrite(skill, pending)
    revision = _current_skill(skill).revision
    _audit_best_effort("approve", skill, revision, actor)
    return result


def _rollback_source(skill: str, revision: str) -> Path:
    """Validate a rollback request and return its snapshot directory."""
    skill = check_slug(skill)
    if not SLUG_RE.fullmatch(revision):
        raise ValueError(f"invalid revision: {revision!r}")
    source = REVISIONS_DIR / skill / revision
    if not source.is_dir():
        raise ValueError(f"no snapshot for '{skill}' at revision {revision}")
    return source


def _stage_rollback(source: Path, skill_dir: Path) -> Path:
    """Copy a rollback snapshot beside the live skill for an atomic rename. A copy that fails part
    way takes its own partial directory with it: a half-copied skill left in the library root would
    otherwise sit there under a name the registry has to know to ignore."""
    stage = skill_dir.with_name(f".{skill_dir.name}.{uuid.uuid4().hex}.rollback")
    try:
        shutil.copytree(source, stage, symlinks=True)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return stage


def _swap_rollback(skill_dir: Path, stage: Path) -> None:
    """Atomically install a staged rollback, restoring the live directory on failure."""
    previous = skill_dir.with_name(f".{skill_dir.name}.{uuid.uuid4().hex}.previous")
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


def rollback(skill: str, revision: str, actor: str = "local-operator") -> str:
    """Atomically restore a snapshot while preserving the displaced current revision."""
    source = _rollback_source(skill, revision)
    current = _current_skill(skill)
    skill_dir = Path(current.root)
    _snapshot(skill_dir, skill, current.revision)
    _sweep_staging(skill_dir)
    stage = _stage_rollback(source, skill_dir)
    _swap_rollback(skill_dir, stage)
    restored = _current_skill(skill)
    _audit_best_effort("rollback", skill, restored.revision, actor)
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
