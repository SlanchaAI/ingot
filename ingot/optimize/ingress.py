"""Quarantine a vetted new skill for human review without adding it to the served library."""
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

from ingot.mcp_server.registry import (SLUG_RE, load_skills, normalized_frontmatter, skill_revision,
                                       writable_skill_dir)
from ingot.optimize import promote, tree
from ingot.optimize.evidence import recorded_path
from ingot import paths



def evidence_dir() -> Path:
    return paths.runs() / "evidence"


def audit_file() -> Path:
    return paths.runs() / "ingress-audit.jsonl"

MAX_FILES = 32
MAX_TOTAL_CHARS = 1_000_000
MAX_FIELD_CHARS = 4_000
MAX_EVIDENCE_ITEMS = 12
TEXT_SUFFIXES = {".md", ".txt", ".py", ".sh", ".js", ".ts", ".json", ".yaml", ".yml",
                 ".toml", ".cfg"}
logger = logging.getLogger(__name__)
_SUBMIT_LOCK = threading.Lock()


def _text(name: str, value: object, *, limit: int = MAX_FIELD_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    return value


def _files(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > MAX_FILES:
        raise ValueError(f"files must be an object with at most {MAX_FILES} entries")
    result = {}
    portable_paths = set()
    for raw_path, content in value.items():
        if not isinstance(raw_path, str) or not isinstance(content, str):
            raise ValueError("file paths and contents must be strings")
        path = tree.portable_path(raw_path)
        folded = path.as_posix().casefold()
        if folded in portable_paths:
            raise ValueError(f"component path collides case-insensitively: {raw_path}")
        portable_paths.add(folded)
        if path.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError(f"unsupported skill file type: {raw_path}")
        result[f"file:{path.as_posix()}"] = content
    if sum(len(item) for item in result.values()) > MAX_TOTAL_CHARS:
        raise ValueError(f"files exceed {MAX_TOTAL_CHARS} total characters")
    return result


def _proposal_id(identity: dict) -> str:
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def _write_evidence(skill: str, proposal: dict, gate: dict) -> dict[str, str]:
    """The bundle a reviewer reads.

    Narrative sections are optional. An agent proposing a skill it authored can state a pressure
    scenario and a verification it ran; an operator ingesting a third-party package cannot, and
    inventing one on their behalf would put a fabricated claim in front of the person whose job is
    to check claims. Absent sections are omitted rather than filled in."""
    root = evidence_dir() / skill / f"creation-{proposal['proposal_id']}"
    root.mkdir(parents=True, exist_ok=True)
    bundle = {"schema_version": "ingot/skill-create/v1", "skill": skill,
              "created": proposal["created"], "proposal": proposal, "gate": gate}
    verification = proposal.get("verification") or {}
    markdown = "\n".join([
        f"# New skill proposal: {skill}", "", f"**Source:** `{proposal['source']}`", "",
        proposal["summary"], "", "## Evidence", "",
        *[f"- {item}" for item in proposal["evidence"]], "",
        *(["## Pressure scenario", "", proposal["pressure_scenario"], ""]
          if proposal.get("pressure_scenario") else []),
        *(["## Verification", "",
           f"- Command: `{verification.get('command', '')}`",
           f"- Result: {verification.get('result', '')}", ""] if verification else []),
        "Human approval is required before this skill enters the served library.", "",
    ])
    paths = ((root / "evidence.json", json.dumps(bundle, indent=2) + "\n"),
             (root / "EVIDENCE.md", markdown))
    for path, content in paths:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    return {"json": recorded_path(paths[0][0]), "markdown": recorded_path(paths[1][0])}


def _publish(skill: str, record: dict) -> bool:
    promote.pending_dir().mkdir(parents=True, exist_ok=True)
    destination = promote.pending_path(skill)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    payload = (json.dumps(record, indent=2) + "\n").encode()
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("pending proposal write made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(temporary, destination)
    except FileExistsError as exc:
        existing = promote.load_pending(skill)
        existing_id = (existing.get("creation") or {}).get("proposal_id") if existing else None
        if existing_id == record["creation"]["proposal_id"]:
            return False
        raise ValueError(f"review slot is occupied for '{skill}'") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _audit(record: dict) -> None:
    audit_file().parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(audit_file(), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        view = memoryview((json.dumps(record, separators=(",", ":")) + "\n").encode())
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("ingress audit write made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _accept_slot(skill: str) -> Path:
    """The skill must be new. Returns the directory it would occupy.

    Shared by every admission front door, so a package cannot enter through one path what another
    would refuse."""
    if not isinstance(skill, str) or len(skill) > 80 or not SLUG_RE.fullmatch(skill):
        raise ValueError(f"invalid skill name: {skill!r}")
    target = writable_skill_dir(skill)
    if target.exists() or target.is_symlink() or any(item.name == skill for item in load_skills()):
        raise ValueError(f"skill '{skill}' already exists; propose an update instead")
    return target


def build_components(skill: str, description: str, body: str, files: dict[str, str],
                     frontmatter: dict) -> tuple[dict[str, str], dict]:
    """The component map a candidate is made of, plus its normalized frontmatter.

    One implementation for every source: path validation, portability, and the field limits are
    properties of what Ingot will serve, not of who proposed it. Public because an ingest adapter
    has to know the candidate revision -- which is a property of these components, not of the
    directory they were read from -- before it can build a candidate manifest."""
    description = " ".join(_text("description", description, limit=2_000).split())
    body = _text("body", body, limit=200_000)
    metadata = normalized_frontmatter(skill, description, frontmatter)
    canonical_frontmatter = json.dumps(metadata, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False)
    if len(canonical_frontmatter) > 20_000:
        raise ValueError("frontmatter exceeds 20000 characters")
    return ({"description": description, "body": body,
             "frontmatter": canonical_frontmatter, **_files(files)}, metadata)


def _quarantine(skill: str, record: dict, proposal: dict, gate: dict) -> dict:
    """Claim the review slot, write the evidence bundle, publish the pending record, audit.

    The only path that creates a pending proposal. `_publish` links the record into place, which is
    atomic across processes, so two submitters racing for one slot cannot both win -- the in-process
    lock below narrows the window but is not what makes it safe."""
    with _SUBMIT_LOCK:
        proposal_id = proposal["proposal_id"]
        existing = promote.load_pending(skill)
        if existing:
            existing_id = (existing.get("creation") or {}).get("proposal_id")
            if existing_id == proposal_id:
                return {"status": "duplicate", "skill": skill, "proposal_id": proposal_id,
                        "promotable": True}
            raise ValueError(f"review slot is occupied for '{skill}'")
        record["evidence_paths"] = _write_evidence(skill, proposal, gate)
        evidence_root = evidence_dir() / skill / f"creation-{proposal_id}"
        try:
            published = _publish(skill, record)
        except Exception:
            shutil.rmtree(evidence_root, ignore_errors=True)
            raise
        if not published:
            return {"status": "duplicate", "skill": skill, "proposal_id": proposal_id,
                    "promotable": True}
        try:
            _audit({"schema_version": 1, "ts": int(time.time()), "action": "quarantine",
                    "skill": skill, "proposal_id": proposal_id,
                    "challenger_revision": proposal["revision"],
                    "producer": proposal["producer"]})
        except Exception:
            logger.warning("Quarantined creation proposal %s, but its audit write failed",
                           proposal_id, exc_info=True)
    return {"status": "quarantined", "skill": skill, "proposal_id": proposal_id,
            "promotable": True}


def submit_package_ingest(*, skill: str, components: dict[str, str], metadata: dict,
                          revision: str, source: str, candidate: dict, identity: str,
                          review_summary: list[str], producer: str, caller: str,
                          candidate_tree: dict) -> dict:
    """Quarantine a package an operator ingested from somewhere else.

    Distinct from `submit_skill_create` in what it is allowed to claim, not in what it does. An
    agent proposing a skill it wrote can attest to a pressure scenario and a verification run; an
    operator pointing at a third-party directory can attest only to where it came from and what the
    deterministic review found. Both produce the same record, take the same slot, and are equally
    inert until a human approves them.

    Also distinct in what it carries. This path has real bytes on disk, so the candidate is a
    staged tree covering every file in the package; `submit_skill_create` receives strings over
    MCP and has no bytes to preserve."""
    _accept_slot(skill)
    tree.verify_manifest(candidate_tree)

    proposal = {
        "skill": skill,
        "revision": revision,
        "source": _text("source", source),
        "summary": f"Ingested package '{skill}' from {candidate['source']['type']}",
        # Real, machine-produced findings. Nothing here is a claim a person made.
        "evidence": review_summary or ["Deterministic review found no findings."],
        "created": candidate["created_at"],
        "producer": _text("producer", producer),
        "caller": _text("caller", caller),
        "candidate": candidate,
        "frontmatter": metadata,
        # Identity is the candidate's, so the same bytes ingested twice are one proposal even
        # though the manifest's timestamp differs.
        "proposal_id": _proposal_id({"candidate_identity": identity}),
    }
    gate = {"promotable": True, "blocked": [],
            "warnings": ["Ingested package; no behavioural evidence and no held-out A/B exists.",
                         *review_summary],
            "kind": "package_ingest"}
    record = {"skill": skill, "kind": "creation", "created": proposal["created"],
              "changed_components": list(components), "champion_components": {},
              "challenger_components": components, "tree": candidate_tree, "gate": gate,
              "evidence": {"schema_version": "ingot/skill-create/v1",
                           "challenger": {"revision": revision}, "gate": gate},
              "creation": proposal}
    return _quarantine(skill, record, proposal, gate)


def submit_skill_create(*, skill: str, description: str, body: str, files: dict[str, str],
                        frontmatter: dict,
                        summary: str, source: str, producer: str, caller: str,
                        evidence: list[str], pressure_scenario: str, risk: str,
                        verification_status: str, verification_command: str,
                        verification_result: str) -> dict:
    """Validate and quarantine one new skill package; never add it to the active registry."""
    target = _accept_slot(skill)
    components, metadata = build_components(skill, description, body, files, frontmatter)
    description = components["description"]
    if not isinstance(verification_status, str) or verification_status.strip().lower() != "passed":
        raise ValueError("verification_status must be passed before proposing")
    if not isinstance(evidence, list) or not 2 <= len(evidence) <= MAX_EVIDENCE_ITEMS:
        raise ValueError(f"evidence must contain 2 to {MAX_EVIDENCE_ITEMS} concrete items")
    evidence = [_text("evidence item", item) for item in evidence]
    if len(set(evidence)) != len(evidence):
        raise ValueError("evidence items must be distinct")

    revision = skill_revision(target, components)
    proposal = {"skill": skill, "revision": revision, "source": _text("source", source),
                "summary": _text("summary", summary), "evidence": evidence,
                "created": int(time.time()),
                "producer": _text("producer", producer), "caller": _text("caller", caller),
                "pressure_scenario": _text("pressure_scenario", pressure_scenario),
                "risk": _text("risk", risk),
                "verification": {"status": "passed",
                                 "command": _text("verification_command", verification_command),
                                 "result": _text("verification_result", verification_result)},
                "frontmatter": metadata}
    identity = {key: value for key, value in proposal.items()
                if key not in {"created", "producer", "caller"}}
    proposal_id = _proposal_id(identity)
    proposal["proposal_id"] = proposal_id
    gate = {"promotable": True, "blocked": [],
            "warnings": ["New skill admission only; no active champion or held-out A/B exists."],
            "kind": "new_skill_admission"}
    record = {"skill": skill, "kind": "creation", "created": proposal["created"],
              "changed_components": list(components), "champion_components": {},
              "challenger_components": components, "gate": gate,
              "evidence": {"schema_version": "ingot/skill-create/v1",
                           "challenger": {"revision": revision}, "gate": gate},
              "creation": proposal}

    return _quarantine(skill, record, proposal, gate)
