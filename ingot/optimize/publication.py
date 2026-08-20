"""Durable, inert approvals waiting for publication into the canonical skill vault."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ingot.mcp_server.registry import SLUG_RE
from ingot.optimize import tree
from ingot import paths




def publications_dir() -> Path:
    """Approved receipts waiting on the publisher. Resolved per call, never bound at import."""
    return paths.runs() / "publications"

ACTIVE_STATES = {"approved_publishing", "publishing", "awaiting_merge", "merged"}
ACTIONS = {"promote", "rollback"}


@dataclass(frozen=True)
class PublicationReceipt:
    id: str
    state: str
    path: Path


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _publication_id(identity: dict) -> str:
    return hashlib.sha256(_canonical(identity).encode()).hexdigest()[:24]


def _proposal_id(pending: dict) -> str:
    for key in ("creation", "retrospective"):
        value = (pending.get(key) or {}).get("proposal_id")
        if isinstance(value, str) and value:
            return value
    value = pending.get("proposal_id")
    return value if isinstance(value, str) else ""


def _components(pending: dict, action: str) -> dict[str, str]:
    """A promotion carries the exact approved bytes; a rollback republishes a stored snapshot.

    The snapshot is the authority for a rollback, so carrying components beside it would create a
    second description of the same target that could disagree with it."""
    raw = pending.get("challenger_components")
    if action == "rollback":
        if raw is not None and not isinstance(raw, dict):
            raise ValueError("challenger components must be an object")
        if raw:
            raise ValueError("a rollback republishes a stored snapshot and carries no components")
        return {}
    if not isinstance(raw, dict) or not raw:
        raise ValueError("challenger components are required for publication")
    result = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("component names and contents must be strings")
        if key.startswith("file:"):
            path = PurePosixPath(key[5:])
            if path.is_absolute() or ".." in path.parts or path.as_posix() in {".", "SKILL.md"}:
                raise ValueError(f"component escapes skill root: {path}")
        elif key not in {"description", "body", "frontmatter"}:
            raise ValueError(f"unsupported component: {key}")
        result[key] = value
    if "description" not in result or "body" not in result:
        raise ValueError("description and body components are required")
    return result


def _tree(pending: dict, action: str) -> dict | None:
    """The staged bytes an ingested package publishes, if it carries any.

    Absent for an optimizer promotion, which rewrites text in a skill the vault already holds, and
    for a rollback, which restores a stored snapshot. Present for an ingested package, where it is
    the authority for every file: the receipt binds the tree's digest, and the publisher refuses
    any staged file whose hash has moved since approval."""
    raw = pending.get("tree")
    if raw is None:
        return None
    if action == "rollback":
        raise ValueError("a rollback republishes a stored snapshot and carries no candidate tree")
    return tree.verify_manifest(raw)


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"invalid publication record: {path}")
    return value


def load_publication(publication_id: str) -> dict | None:
    if not SLUG_RE.fullmatch(publication_id):
        raise ValueError(f"invalid publication id: {publication_id!r}")
    path = publications_dir() / f"{publication_id}.json"
    return _read(path) if path.is_file() else None


def update_publication(publication_id: str, **changes) -> dict:
    """Replace one receipt durably; the publication id and candidate identity cannot change."""
    path = publications_dir() / f"{publication_id}.json"
    record = _read(path)
    immutable = {"id", "skill", "action", "expected_champion", "candidate_revision", "components",
                 "tree"}
    if immutable.intersection(changes):
        raise ValueError("publication identity is immutable")
    record.update(changes)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, (_canonical(record) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return record


def _all_records() -> list[dict]:
    """Every readable receipt, newest first. A receipt this process cannot parse is skipped
    rather than fatal: one corrupt file must not blank the whole lane."""
    if not publications_dir().is_dir():
        return []
    records = []
    for path in publications_dir().glob("*.json"):
        try:
            records.append(_read(path))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    records.sort(key=_ordering, reverse=True)
    return records


def publication_for_skill(skill: str) -> dict | None:
    for record in _all_records():
        if record.get("skill") == skill:
            return record
    return None


LIVE_STATES = ("approved_publishing", "publishing", "awaiting_merge")


def publishing_skills() -> set[str]:
    """Skills whose newest receipt is still travelling to the vault.

    One pass over the store, not one per skill: the board asks this for every skill it lists.
    Only the newest receipt counts, or a skill would look like it were publishing forever on the
    strength of some earlier attempt."""
    newest: dict[str, dict] = {}
    for record in _all_records():
        newest.setdefault(record.get("skill", ""), record)
    return {skill for skill, record in newest.items() if record.get("state") in LIVE_STATES}


def latest_releases() -> dict[str, dict]:
    """The newest successful release per skill: what the deployment is supposed to be serving.

    One pass over the store, like `publishing_skills`, because status asks this for every skill it
    lists. Only an `active` receipt counts — a receipt that failed on its way to the vault never
    described served bytes and must not be mistaken for a release."""
    releases: dict[str, dict] = {}
    for record in _all_records():
        if record.get("state") == "active":
            releases.setdefault(record.get("skill", ""), record)
    return releases


def recent_publications(limit: int = 12) -> list[dict]:
    """The publication lane as a whole, newest first.

    `publication_for_skill` only answers for a skill that still has a pending record, so once a
    change is approved the console loses sight of it — which is exactly the window where it is
    waiting on a vault merge and a person needs to see it."""
    return _all_records()[:max(0, limit)]


def _ordering(record: dict) -> tuple:
    """Newest first. `created` is whole seconds, so a rollback queued in the same second as the
    approval it displaces would otherwise be ordered by the hash in its id — and the review surface
    would render whichever receipt happened to sort higher."""
    created = record.get("created_ns")
    if not isinstance(created, int) or isinstance(created, bool):
        created = int(record.get("created", 0) or 0) * 1_000_000_000
    return (created, record.get("id", ""))


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("publication write made no progress")
        view = view[written:]


def _publish(path: Path, record: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, (_canonical(record) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def queue_publication(skill: str, pending: dict, actor: str, action: str) -> PublicationReceipt:
    """Record one approved candidate without changing any served skill or pending review slot."""
    if not SLUG_RE.fullmatch(skill):
        raise ValueError(f"invalid skill name: {skill!r}")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("approval actor is required")
    if action not in ACTIONS:
        raise ValueError(f"invalid publication action: {action!r}")
    evidence = pending.get("evidence") or {}
    expected = ((evidence.get("champion") or {}).get("revision")
                if pending.get("kind") != "creation" else "absent")
    candidate = (evidence.get("challenger") or {}).get("revision")
    if not isinstance(expected, str) or not expected:
        raise ValueError("expected champion revision is required")
    if not isinstance(candidate, str) or not candidate:
        raise ValueError("candidate revision is required")
    components = _components(pending, action)
    candidate_tree = _tree(pending, action)
    proposal_id = _proposal_id(pending)
    identity = {"skill": skill, "action": action, "expected_champion": expected,
                "candidate_revision": candidate, "components": components}
    if candidate_tree:
        identity["tree"] = candidate_tree["digest"]
    publication_id = _publication_id(identity)
    path = publications_dir() / f"{publication_id}.json"
    record = {
        "schema_version": "ingot/publication/v1",
        "id": publication_id,
        "skill": skill,
        "state": "approved_publishing",
        "proposal_id": proposal_id,
        "actor": actor.strip(),
        "action": action,
        "kind": pending.get("kind", "quality"),
        "expected_champion": expected,
        "candidate_revision": candidate,
        "components": components,
        "created": int(time.time()),
        "created_ns": time.time_ns(),
        "attempts": 0,
        "last_error": "",
    }
    if candidate_tree:
        record["tree"] = candidate_tree

    publications_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(publications_dir(), 0o700)
    lock_path = publications_dir() / ".queue.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if path.is_file():
            existing = _read(path)
            return PublicationReceipt(publication_id, existing["state"], path)
        occupied = publication_for_skill(skill)
        if occupied and occupied.get("state") in ACTIVE_STATES:
            raise ValueError(f"publication is already in progress for '{skill}'")
        try:
            _publish(path, record)
        except FileExistsError:
            existing = _read(path)
            return PublicationReceipt(publication_id, existing["state"], path)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    return PublicationReceipt(publication_id, record["state"], path)
