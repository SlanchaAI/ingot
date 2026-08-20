"""Turn a skill-retrospective finding into an inert, revision-bound Ingot challenger."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from ingot.mcp_server.registry import (load_skills, optimizable_components, resolve_skill_dir,
                                       skill_revision)
from ingot.optimize import promote
from ingot.optimize.evidence import recorded_path
from ingot import paths

SCHEMA = "ingot/retrospective-proposal/v1"


def evidence_dir() -> Path:
    return paths.runs() / "evidence"


def audit_file() -> Path:
    return paths.runs() / "retrospective-audit.jsonl"

MAX_BODY_CHARS = 200_000
MAX_FIELD_CHARS = 4_000
MAX_EVIDENCE_ITEMS = 12

logger = logging.getLogger(__name__)
_SUBMIT_LOCK = threading.Lock()


def _text(name: str, value: object, *, required: bool = True,
          limit: int = MAX_FIELD_CHARS) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{name} is required")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    return value


def _evidence_items(items: object) -> list[str]:
    if not isinstance(items, list) or len(items) < 2:
        raise ValueError("evidence must contain at least two concrete repeat items")
    if len(items) > MAX_EVIDENCE_ITEMS:
        raise ValueError(f"evidence exceeds {MAX_EVIDENCE_ITEMS} items")
    evidence = [_text(f"evidence[{index}]", item) for index, item in enumerate(items)]
    if len(set(evidence)) != len(evidence):
        raise ValueError("evidence items must be distinct")
    return evidence


def _current_skill(skill: str):
    try:
        skill_dir = resolve_skill_dir(skill)
    except LookupError as exc:
        raise ValueError(f"no indexed skill named '{skill}'") from exc
    current = next((item for item in load_skills() if item.name == skill), None)
    if current is None:
        raise ValueError(f"no indexed skill named '{skill}'")
    return current, skill_dir


def _proposal_id(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def _gate(evidence: list[str]) -> dict:
    """Record the admission basis without presenting it as held-out quality evidence."""
    return {
        "promotable": True,
        "blocked": [],
        "warnings": ["Retrospective evidence only; no held-out A/B comparison was run."],
        "kind": "retrospective_admission",
        "admission": {"pressure_verification": "passed", "evidence_items": len(evidence)},
    }


def _render_markdown(skill: str, proposal: dict, gate: dict) -> str:
    verification = proposal["verification"]
    lines = [
        f"# Retrospective proposal: {skill}",
        "",
        f"**Schema:** `{SCHEMA}`",
        f"**Proposal:** `{proposal['proposal_id']}`",
        f"**Gate:** {'PASS' if gate['promotable'] else 'BLOCKED'}",
        f"**Producer:** {proposal['producer']}",
        f"**Live caller:** {proposal['caller']}",
        "",
        "## Finding",
        "",
        proposal["summary"],
        "",
        f"- Trigger: {proposal['trigger']}",
        f"- Minimal reusable content: {proposal['minimal_content']}",
        f"- Risk: {proposal['risk']}",
        "",
        "## Evidence",
        "",
        *[f"- {item}" for item in proposal["evidence"]],
        "",
        "## Pressure scenario",
        "",
        proposal["pressure_scenario"],
        "",
        "## Verification",
        "",
        f"- Status: **{verification['status'].upper()}**",
        f"- Command: `{verification['command']}`",
        f"- Result: {verification['result']}",
        "",
        "Submission only quarantines this challenger. Human approval remains required.",
        "",
    ]
    return "\n".join(lines)


def _write_evidence(skill: str, proposal: dict, gate: dict) -> dict[str, str]:
    root = evidence_dir() / skill / f"retrospective-{proposal['proposal_id']}"
    root.mkdir(parents=True, exist_ok=True)
    bundle = {
        "schema_version": SCHEMA,
        "skill": skill,
        "created": proposal["created"],
        "proposal": proposal,
        "gate": gate,
    }
    targets = (
        (root / "evidence.json", json.dumps(bundle, indent=2, ensure_ascii=False) + "\n"),
        (root / "EVIDENCE.md", _render_markdown(skill, proposal, gate)),
    )
    for path, content in targets:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    return {"json": recorded_path(targets[0][0]), "markdown": recorded_path(targets[1][0])}


def _publish_pending(skill: str, record: dict) -> None:
    """Create the review slot without replacing a writer that wins the race.

    `save_pending` intentionally archives and replaces another pass. Retrospective submissions have
    weaker authority: an agent may propose, but it may not displace something a human can review.
    A hard link gives that policy an atomic create-if-absent operation on the queue filesystem.
    """
    promote.pending_dir().mkdir(parents=True, exist_ok=True)
    destination = promote.pending_path(skill)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as output:
        output.write(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())
    try:
        os.link(temporary, destination)
    except FileExistsError:
        raise ValueError(f"review slot is occupied for '{skill}'; resolve it before proposing")
    finally:
        temporary.unlink(missing_ok=True)


def _audit(record: dict) -> None:
    audit_file().parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(audit_file(), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.fchmod(fd, 0o600)
        data = (json.dumps(record, separators=(",", ":")) + "\n").encode()
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.fsync(fd)
    finally:
        os.close(fd)


def submit_skill_update(
    *,
    skill: str,
    champion_revision: str,
    challenger_body: str,
    challenger_description: str = "",
    summary: str,
    trigger: str,
    minimal_content: str,
    producer: str,
    caller: str,
    evidence: list[str],
    pressure_scenario: str,
    risk: str,
    verification_status: str,
    verification_command: str,
    verification_result: str,
) -> dict:
    """Validate and quarantine one retrospective update; never activate or replace another slot."""
    skill = _text("skill", skill, limit=80)
    champion_revision = _text("champion_revision", champion_revision, limit=128)
    challenger_body = _text("challenger_body", challenger_body, limit=MAX_BODY_CHARS)
    challenger_description = _text(
        "challenger_description", challenger_description, required=False, limit=2_000)
    verification_status = _text(
        "verification_status", verification_status, limit=20).lower()
    if verification_status != "passed":
        raise ValueError("verification_status must be passed before proposing")

    current, skill_dir = _current_skill(skill)
    if current.revision != champion_revision:
        raise ValueError("champion revision changed; reload the skill before proposing")

    champion = optimizable_components(skill_dir)
    challenger = dict(champion)
    challenger["body"] = challenger_body
    if challenger_description:
        challenger["description"] = challenger_description
    changed = sorted(key for key in challenger if challenger.get(key) != champion.get(key))
    if not changed:
        raise ValueError("retrospective proposal does not change the skill")

    created = int(time.time())
    proposal = {
        "schema_version": SCHEMA,
        "created": created,
        "summary": _text("summary", summary),
        "trigger": _text("trigger", trigger),
        "minimal_content": _text("minimal_content", minimal_content),
        "producer": _text("producer", producer, limit=200),
        "caller": _text("caller", caller, limit=500),
        "evidence": _evidence_items(evidence),
        "pressure_scenario": _text("pressure_scenario", pressure_scenario),
        "risk": _text("risk", risk),
        "verification": {
            "status": verification_status,
            "command": _text("verification_command", verification_command),
            "result": _text("verification_result", verification_result),
        },
    }
    # Submission time is evidence metadata, not proposal identity. Excluding it makes an exact
    # retry idempotent even when a transport retries after the clock advances.
    identity = {
        **{key: value for key, value in proposal.items()
           if key not in {"created", "producer", "caller"}},
        "skill": skill,
        "champion_revision": champion_revision,
        "challenger_revision": skill_revision(skill_dir, challenger),
    }
    proposal["proposal_id"] = _proposal_id(identity)
    gate = _gate(proposal["evidence"])
    record = {
        "skill": skill,
        "kind": "retrospective",
        "created": created,
        "changed_components": changed,
        "champion_components": champion,
        "challenger_components": challenger,
        "gate": gate,
        "evidence": {
            "schema_version": SCHEMA,
            "champion": {"revision": champion_revision},
            "challenger": {"revision": identity["challenger_revision"]},
            "gate": gate,
        },
        "retrospective": proposal,
    }

    with _SUBMIT_LOCK:
        existing = promote.load_pending(skill)
        if existing:
            existing_id = (existing.get("retrospective") or {}).get("proposal_id")
            if existing_id == proposal["proposal_id"]:
                return {"status": "duplicate", "skill": skill,
                        "proposal_id": proposal["proposal_id"], "promotable": gate["promotable"]}
            raise ValueError(f"review slot is occupied for '{skill}'; resolve it before proposing")
        record["evidence_paths"] = _write_evidence(skill, proposal, gate)
        evidence_root = evidence_dir() / skill / f"retrospective-{proposal['proposal_id']}"
        try:
            _publish_pending(skill, record)
        except ValueError:
            current = promote.load_pending(skill)
            current_id = ((current or {}).get("retrospective") or {}).get("proposal_id")
            if current_id == proposal["proposal_id"]:
                return {"status": "duplicate", "skill": skill,
                        "proposal_id": proposal["proposal_id"], "promotable": gate["promotable"]}
            shutil.rmtree(evidence_root, ignore_errors=True)
            raise
        except OSError as exc:
            shutil.rmtree(evidence_root, ignore_errors=True)
            raise RuntimeError(
                f"cannot atomically publish the review slot for '{skill}'") from exc
        try:
            _audit({"schema_version": 1, "ts": int(time.time()), "action": "quarantine",
                    "skill": skill, "proposal_id": proposal["proposal_id"],
                    "challenger_revision": identity["challenger_revision"],
                    "producer": proposal["producer"]})
        except Exception:
            logger.warning("Quarantined retrospective proposal %s, but its audit write failed",
                           proposal["proposal_id"], exc_info=True)

    return {"status": "quarantined", "skill": skill,
            "proposal_id": proposal["proposal_id"], "promotable": gate["promotable"]}
