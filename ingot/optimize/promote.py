"""Gate-enforced, revisioned, atomic promotion and rollback for quarantined skill changes."""
from __future__ import annotations

import ctypes
import errno
import json
import logging
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

from ingot.mcp_server.registry import (SLUG_RE, load_skills, read_components, skill_revision,
                                       writable_skill_dir, write_components)
from ingot import paths



def pending_dir() -> Path:
    """The one review slot per skill. Resolved per call: it is configuration, and a value frozen at
    import cannot follow a process that is told where its state lives."""
    return paths.runs() / "pending"


def revisions_dir() -> Path:
    """Stored snapshots, the rollback targets."""
    return paths.runs() / "revisions"

ABSENT_REVISION = "absent"
ABSENT_MARKER = ".ingot-absent"
logger = logging.getLogger(__name__)


def check_slug(skill: str) -> str:
    if not SLUG_RE.fullmatch(skill):
        raise ValueError(f"invalid skill name: {skill!r}")
    return skill


def pending_path(skill: str) -> Path:
    return pending_dir() / f"{check_slug(skill)}.json"


def save_pending(skill: str, data: dict) -> Path:
    pending_dir().mkdir(parents=True, exist_ok=True)
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
    archived = pending_dir() / f"{skill}.displaced-{existing.get('created', uuid.uuid4().hex)}.json"
    shutil.copy(path, archived)
    print(f"[pending] one review slot per skill: the pending "
          f"{existing.get('changed_components')} challenger was displaced by this "
          f"{data.get('changed_components')} challenger and archived to {archived} "
          f"(promote or reject before running a different pass to avoid this)")


def load_pending(skill: str) -> dict | None:
    path = pending_path(skill)
    return json.loads(path.read_text()) if path.exists() else None


def unreadable_pending() -> list[str]:
    """Pending files this process can see but cannot use.

    `list_pending` skips them so one corrupt record cannot break review — which also means a
    proposal this process cannot read is indistinguishable from no proposal at all, and the console
    reports CLEAR over it. Observed live: the MCP container writes records as root 0600 while the UI
    runs as uid 1000, so an approved-and-waiting skill was invisible for hours."""
    if not pending_dir().exists():
        return []
    blocked = []
    for path in sorted(pending_dir().glob("*.json")):
        if not SLUG_RE.fullmatch(path.stem):
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):   # ValueError covers JSONDecodeError and UnicodeDecodeError
            blocked.append(path.name)
            continue
        if not (isinstance(record, dict) and record.get("skill") == path.stem):
            blocked.append(path.name)
    return blocked


def list_pending() -> list[dict]:
    """Return valid pending records without letting a malformed queue file break the review UI."""
    if not pending_dir().exists():
        return []
    records = []
    for path in sorted(pending_dir().glob("*.json")):
        if not SLUG_RE.fullmatch(path.stem):
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            # ValueError, not json.JSONDecodeError: a non-UTF-8 file raises UnicodeDecodeError,
            # which escaped this handler and took the whole review page down with it.
            continue
        if isinstance(record, dict) and record.get("skill") == path.stem:
            records.append(record)
    return records


def snapshot_index_path(skill: str) -> Path:
    """When each snapshot was last taken. It lives beside the snapshot directories, never inside
    one, so a rollback copies back the skill and nothing else. The leading dot also keeps it out of
    the slug-matched snapshot listing."""
    return revisions_dir() / check_slug(skill) / ".snapshots.json"


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
    root = revisions_dir() / check_slug(skill)
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


def load_snapshot_components(skill: str, revision: str) -> dict[str, str]:
    """Read one validated rollback snapshot for the read-only version explorer."""
    return read_components(_rollback_source(skill, revision))


def list_snapshotted_skills() -> list[str]:
    """Skills with at least one snapshot. Reading the snapshot store directly keeps the history
    view off the skill-library hash scan that the skills listing already pays for."""
    if not revisions_dir().is_dir():
        return []
    return sorted(path.name for path in revisions_dir().iterdir()
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
    if pending.get("kind") == "creation":
        if any(item.name == skill for item in load_skills()):
            return "a skill with this name appeared after the proposal; review it as an update"
        expected = evidence.get("challenger", {}).get("revision")
        components = pending.get("challenger_components", {})
        manifest = pending.get("tree")
        # An ingested package's revision is what materializing its staged tree produces, not what
        # its decoded text components produce -- the tree carries files no component describes.
        # Recomputing from the components alone would report every package with a second file as
        # permanently stale: quarantined, reviewable, and impossible to approve.
        if manifest is not None:
            from ingot.optimize import tree as candidate_tree
            try:
                actual = candidate_tree.revision(skill, manifest, components)
            except (ValueError, OSError) as exc:
                return f"the staged candidate tree is no longer usable: {exc}"
        else:
            actual = skill_revision(writable_skill_dir(skill), components)
        return None if expected == actual else "challenger revision does not match the recorded evidence"
    try:
        current = _current_skill(skill)
    except ValueError as exc:
        return str(exc)
    return _revision_problem(current, pending.get("challenger_components", {}), evidence)


def _snapshot(skill_dir: Path, skill: str, revision: str) -> Path:
    """Preserve a revision as a rollback target. Re-snapshotting a revision that is already stored
    is a no-op on disk but still restamps it: that is what a rollback followed by a promotion does,
    and the restored revision is then the most recent thing a promotion displaced."""
    destination = revisions_dir() / skill / revision
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
    return pending_dir().parent / "approval-audit.jsonl"


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


def _audit(action: str, skill: str, revision: str, actor: str = "local-operator",
           reason: str = "") -> None:
    """Append transition metadata without recording skill bodies or credentials."""
    audit_file = audit_path()
    audit_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(audit_file.parent, 0o700)
    fd = os.open(audit_file, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        record = {"schema_version": 1, "ts": int(time.time()), "action": action,
                  "skill": skill, "revision": revision, "actor": actor}
        if reason:
            record["reason"] = reason
        _write_all(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode())
    finally:
        os.close(fd)


def _audit_best_effort(action: str, skill: str, revision: str, actor: str,
                       reason: str = "") -> None:
    """Record a committed transition without changing its successful outcome."""
    try:
        _audit(action, skill, revision, actor, reason)
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


def _rename_no_replace(source: Path, target: Path) -> None:
    """Atomically publish a new skill directory without replacing a target that won a race.

    Python's POSIX rename replaces an empty directory, so an existence check followed by rename is
    not a guard. Linux and macOS expose the needed no-replace operation under different names; fail
    closed on other POSIX platforms rather than quietly restore the race.
    """
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                           ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    elif sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(source), os.fsencode(target), 0x00000004)
    elif os.name == "nt":
        source.rename(target)  # Windows rename already refuses an existing destination.
        return
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def _activate_rewrite(skill: str, pending: dict) -> str:
    components = pending["challenger_components"]
    evidence = pending.get("evidence")
    if not evidence:
        raise ValueError("evidence is required for promotion")

    current = _current_skill(skill)
    _validate_evidence(current, components, evidence)
    source_dir = Path(current.root)
    skill_dir = writable_skill_dir(skill)
    if source_dir != skill_dir and (skill_dir.exists() or skill_dir.is_symlink()):
        raise ValueError(
            f"writable activation target already exists but is not the serving skill: {skill_dir}")
    _snapshot(source_dir, skill, current.revision)
    _sweep_staging(skill_dir)

    stage = skill_dir.with_name(f".{skill_dir.name}.{uuid.uuid4().hex}.stage")
    previous = skill_dir.with_name(f".{skill_dir.name}.{uuid.uuid4().hex}.previous")
    try:
        shutil.copytree(source_dir, stage, symlinks=True)
        write_components(stage, components)
        if source_dir != skill_dir:
            try:
                _rename_no_replace(stage, skill_dir)
            except OSError as exc:
                if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                    raise ValueError(
                        f"writable activation target appeared during promotion: {skill_dir}") from exc
                raise
        else:
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


def _snapshot_absence(skill: str) -> None:
    destination = revisions_dir() / skill / ABSENT_REVISION
    destination.mkdir(parents=True, exist_ok=True)
    (destination / ABSENT_MARKER).touch(exist_ok=True)
    _stamp_snapshot_best_effort(skill, ABSENT_REVISION)


def _activate_creation(skill: str, pending: dict) -> str:
    """Atomically add a reviewed skill while preserving absence as its rollback target."""
    if any(item.name == skill for item in load_skills()):
        raise ValueError(f"skill '{skill}' already exists; review it as an update")
    problem = stale_evidence_reason(skill, pending)
    if problem:
        raise ValueError(problem)
    components = pending["challenger_components"]
    skill_dir = writable_skill_dir(skill)
    if skill_dir.exists() or skill_dir.is_symlink():
        raise ValueError(f"writable activation target already exists: {skill_dir}")
    skill_dir.parent.mkdir(parents=True, exist_ok=True)
    _snapshot_absence(skill)
    _sweep_staging(skill_dir)
    stage = skill_dir.with_name(f".{skill_dir.name}.{uuid.uuid4().hex}.stage")
    try:
        stage.mkdir()
        from ingot.mcp_server.registry import write_skill_md
        try:
            frontmatter = json.loads(components.get("frontmatter", "{}"))
        except (TypeError, ValueError) as exc:
            raise ValueError("creation frontmatter is not valid JSON") from exc
        write_skill_md(stage / "SKILL.md", frontmatter, components["body"])
        write_components(stage, components)
        expected = pending.get("evidence", {}).get("challenger", {}).get("revision")
        actual = skill_revision(stage)
        if actual != expected:
            raise ValueError("staged creation revision does not match the recorded evidence")
        _rename_no_replace(stage, skill_dir)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    pending_path(skill).unlink(missing_ok=True)
    return f"Added '{skill}' to the served library; prior state was absence."


def _activate_approved(skill: str, pending: dict, actor: str = "local-operator") -> str:
    """Complete a publisher-verified approval. The UI approval path must never call this."""
    skill = check_slug(skill)
    _require_promotable(pending)
    result = (_activate_creation(skill, pending) if pending.get("kind") == "creation"
              else _activate_rewrite(skill, pending))
    revision = _current_skill(skill).revision
    _audit_best_effort("approve", skill, revision, actor)
    return result


def approve_pending(skill: str, actor: str = "local-operator") -> str:
    """Approve one tested challenger for vault publication without activating it."""
    skill = check_slug(skill)
    pending = load_pending(skill)
    if not pending:
        raise ValueError(f"no pending challenger for '{skill}'")
    _require_promotable(pending)
    problem = stale_evidence_reason(skill, pending)
    if problem:
        raise ValueError(problem)
    from ingot.optimize.publication import queue_publication
    queue_publication(skill, pending, actor, "promote")
    return f"Approved '{skill}'; publishing to vault."


def challenger_revision(pending: dict) -> str:
    """The challenger's revision from a pending record's evidence, or '' when none is recorded."""
    revision = ((pending.get("evidence") or {}).get("challenger") or {}).get("revision")
    return revision if isinstance(revision, str) else ""


def reject_pending(skill: str, actor: str = "local-operator", reason: str = "") -> str:
    """Discard one quarantined change and record why.

    Shared by the console and the command line so a rejection means the same thing and lands in the
    same trail whichever one a reviewer used. Callers that run concurrently -- the console's request
    threads -- serialize around this themselves; loading, deleting and auditing must happen under
    one lock or a second rejection re-deletes and double-audits after the first releases."""
    skill = check_slug(skill)
    pending = load_pending(skill)
    if pending is None:
        raise ValueError(f"no pending change for '{skill}'")
    revision = challenger_revision(pending)
    pending_path(skill).unlink(missing_ok=True)
    _audit_best_effort("reject", skill, revision, actor, reason=" ".join(reason.split()))
    return f"rejected the pending change for '{skill}'"


def _rollback_source(skill: str, revision: str) -> Path:
    """Validate a rollback request and return its snapshot directory."""
    skill = check_slug(skill)
    if not SLUG_RE.fullmatch(revision):
        raise ValueError(f"invalid revision: {revision!r}")
    source = revisions_dir() / skill / revision
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
    """Queue a stored snapshot for vault publication without changing any served byte.

    Rollback takes the same Git lane as approval: the served library is a read-only checkout of the
    canonical vault, so restoring a revision has to travel through a merged vault commit rather
    than through the filesystem beneath it."""
    _rollback_source(skill, revision)
    try:
        expected = _current_skill(skill).revision
    except ValueError:
        if revision == ABSENT_REVISION:
            raise ValueError(f"skill '{skill}' is already absent") from None
        expected = ABSENT_REVISION
    if expected == revision:
        raise ValueError(f"'{skill}' already serves revision {revision}")
    from ingot.optimize.publication import queue_publication
    queue_publication(skill, {
        "skill": skill,
        "kind": "rollback",
        "challenger_components": {},
        "evidence": {"champion": {"revision": expected},
                     "challenger": {"revision": revision}},
    }, actor, "rollback")
    return f"Approved rollback of '{skill}' to {revision}; publishing to vault."


def _activate_rollback(skill: str, revision: str, actor: str = "local-operator") -> str:
    """Complete a publisher-verified rollback against a writable library. The UI must never call
    this: with the canonical vault mounted read-only, the merged vault commit is what restores a
    revision. It has no production caller today (see the session log)."""
    source = _rollback_source(skill, revision)
    try:
        current = _current_skill(skill)
    except ValueError:
        if (source / ABSENT_MARKER).is_file():
            raise ValueError(f"skill '{skill}' is already absent")
        skill_dir = writable_skill_dir(skill)
        if skill_dir.exists() or skill_dir.is_symlink():
            raise ValueError(f"inactive skill target already exists: {skill_dir}")
        skill_dir.parent.mkdir(parents=True, exist_ok=True)
        _sweep_staging(skill_dir)
        stage = _stage_rollback(source, skill_dir)
        try:
            _rename_no_replace(stage, skill_dir)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        restored = _current_skill(skill)
        _audit_best_effort("rollback", skill, restored.revision, actor)
        return f"Restored absent skill '{skill}' at revision {restored.revision}."
    skill_dir = Path(current.root)
    _snapshot(skill_dir, skill, current.revision)
    _sweep_staging(skill_dir)
    if (source / ABSENT_MARKER).is_file():
        if skill_dir != writable_skill_dir(skill):
            raise ValueError(f"cannot remove mounted skill '{skill}' through an absence rollback")
        previous = skill_dir.with_name(f".{skill_dir.name}.{uuid.uuid4().hex}.previous")
        skill_dir.rename(previous)
        try:
            shutil.rmtree(previous)
        except BaseException:
            previous.rename(skill_dir)
            raise
        _audit_best_effort("rollback", skill, ABSENT_REVISION, actor)
        return f"Rolled back '{skill}' from {current.revision} to absence."
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
