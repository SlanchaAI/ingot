"""Read models and decision verbs for the command line.

Every mutating verb here calls the same service the console calls. There is no second approval
path, no direct activation helper, and nothing that writes a served byte: approval and rollback
queue a publication receipt, and the publisher is what acts on it."""
from __future__ import annotations

PENDING_SCHEMA = "ingot/pending/v1"
HISTORY_SCHEMA = "ingot/history/v1"
DECISION_SCHEMA = "ingot/decision/v1"

APPROVED = "approved"      # a receipt exists; the publisher has not picked it up yet
PUBLISHING = "publishing"  # in flight, including waiting on a forge merge
PUBLISHED = "published"    # the served library carries it
FAILED = "failed"          # the last attempt errored; the receipt is retryable


def release_status(record: dict | None) -> dict | None:
    """One receipt, in the four words a person making a decision needs.

    `failed` reads off `last_error` rather than the state, because a failed attempt leaves the
    receipt in whatever state it was working through. A receipt that reports only its state hides
    the one thing an operator has to act on."""
    if not record:
        return None
    state = record.get("state")
    if state == "active":
        status = PUBLISHED
    elif record.get("last_error"):
        status = FAILED
    elif state in {"publishing", "awaiting_merge"}:
        status = PUBLISHING
    else:
        status = APPROVED
    return {"id": record.get("id"), "status": status, "state": state,
            "action": record.get("action"), "attempts": record.get("attempts", 0),
            "error": record.get("last_error") or "", "revision": record.get("candidate_revision")}


def pending_view() -> dict:
    """Everything waiting on a person, with whatever is already travelling to the vault."""
    from ingot.optimize.promote import challenger_revision, list_pending, unreadable_pending
    from ingot.optimize.publication import publication_for_skill, recent_publications

    entries = []
    for record in sorted(list_pending(), key=lambda item: item.get("skill", "")):
        skill = record.get("skill", "")
        gate = record.get("gate") or {}
        entries.append({
            "skill": skill,
            "kind": record.get("kind", "quality"),
            "promotable": gate.get("promotable") is True,
            "blocked": list(gate.get("blocked") or []),
            "revision": challenger_revision(record),
            "publication": release_status(publication_for_skill(skill)),
        })
    # A receipt outlives the pending record it came from, so an approved change is invisible to the
    # queue above for exactly the window in which it is travelling and someone might be waiting.
    queued = {entry["skill"] for entry in entries}
    travelling = [release_status(record) | {"skill": record.get("skill")}
                  for record in recent_publications()
                  if record.get("skill") not in queued and record.get("state") != "active"]
    return {"schema_version": PENDING_SCHEMA, "pending": entries, "publishing": travelling,
            "unreadable": unreadable_pending()}


def history_view(skill: str, limit: int = 50) -> dict:
    """What this skill has been, what it is travelling toward, and what was decided about it."""
    from ingot.optimize.promote import check_slug, list_revisions, read_audit
    from ingot.optimize.publication import recent_publications

    skill = check_slug(skill)
    audit = [record for record in read_audit(limit=10_000)["records"]
             if record.get("skill") == skill][:limit]
    return {
        "schema_version": HISTORY_SCHEMA,
        "skill": skill,
        "revisions": list_revisions(skill),
        "publications": [release_status(record) for record in recent_publications(10_000)
                         if record.get("skill") == skill][:limit],
        "audit": audit,
    }


def _decision(skill: str, message: str) -> dict:
    from ingot.optimize.publication import publication_for_skill

    return {"schema_version": DECISION_SCHEMA, "skill": skill, "result": message,
            "publication": release_status(publication_for_skill(skill))}


def approve(skill: str, actor: str) -> dict:
    from ingot.optimize.promote import approve_pending

    return _decision(skill, approve_pending(skill, actor=actor))


def reject(skill: str, actor: str, reason: str = "") -> dict:
    from ingot.optimize.promote import reject_pending

    return _decision(skill, reject_pending(skill, actor=actor, reason=reason))


def rollback(skill: str, revision: str, actor: str) -> dict:
    from ingot.optimize.promote import rollback as rollback_pending

    return _decision(skill, rollback_pending(skill, revision, actor=actor))
