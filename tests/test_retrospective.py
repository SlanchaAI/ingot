import json

import pytest

from ingot.mcp_server.registry import load_skills
from ingot.optimize import promote as P
from ingot.optimize import retrospective as R


def _library(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    skill = root / "pdf"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: Merge PDFs.\n---\nold body\n")
    monkeypatch.setenv("INGOT_LIBRARY", str(root))
    monkeypatch.setenv("INGOT_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(root))
    return root, skill


def _proposal(root, **overrides):
    current = load_skills(root)[0]
    data = {
        "skill": "pdf",
        "champion_revision": current.revision,
        "challenger_body": "new body with a reusable guard",
        "challenger_description": "",
        "summary": "Preserve the recovery check after repeated omissions.",
        "trigger": "Use when the same recovery step fails twice.",
        "minimal_content": "Require the recovery check before completion.",
        "producer": "skill-retrospective",
        "caller": "build-loop after repeat evidence",
        "evidence": ["Run A skipped the check.", "Run B repeated the same omission."],
        "pressure_scenario": "A rushed repair reaches completion without checking recovery.",
        "risk": "The extra gate may slow low-risk repairs.",
        "verification_status": "passed",
        "verification_command": "pytest tests/scenarios.md -k recovery",
        "verification_result": "pressure scenario passed",
    }
    data.update(overrides)
    return data


def test_submit_quarantines_a_revision_bound_retrospective(tmp_path, monkeypatch):
    root, _ = _library(tmp_path, monkeypatch)

    result = R.submit_skill_update(**_proposal(root))

    pending = P.load_pending("pdf")
    assert result == {
        "status": "quarantined", "skill": "pdf",
        "proposal_id": pending["retrospective"]["proposal_id"], "promotable": True}
    assert pending["kind"] == "retrospective"
    assert pending["challenger_components"]["description"] == "Merge PDFs."
    assert pending["challenger_components"]["body"] == "new body with a reusable guard"
    assert pending["changed_components"] == ["body"]
    assert pending["gate"] == {
        "promotable": True,
        "blocked": [],
        "warnings": ["Retrospective evidence only; no held-out A/B comparison was run."],
        "kind": "retrospective_admission",
        "admission": {"pressure_verification": "passed", "evidence_items": 2},
    }
    assert pending["evidence"]["champion"]["revision"] == _proposal(root)["champion_revision"]
    assert pending["evidence"]["challenger"]["revision"]
    assert pending["retrospective"]["evidence"] == [
        "Run A skipped the check.", "Run B repeated the same omission."]
    paths = pending["evidence_paths"]
    assert (tmp_path / paths["json"]).exists()
    markdown = (tmp_path / paths["markdown"]).read_text()
    assert "Retrospective proposal: pdf" in markdown
    assert "pressure scenario passed" in markdown
    audit = json.loads((tmp_path / "runs" / "retrospective-audit.jsonl").read_text())
    assert audit["action"] == "quarantine" and audit["proposal_id"] == result["proposal_id"]


def test_metadata_retry_is_idempotent_and_different_pending_is_preserved(tmp_path, monkeypatch):
    root, _ = _library(tmp_path, monkeypatch)
    proposal = _proposal(root)
    now = [100]
    monkeypatch.setattr(R.time, "time", lambda: now[0])
    first = R.submit_skill_update(**proposal)
    before = P.pending_path("pdf").read_text()
    now[0] = 101

    duplicate = R.submit_skill_update(**{**proposal, "caller": "same retry from a new session"})

    assert duplicate == {**first, "status": "duplicate"}
    assert P.pending_path("pdf").read_text() == before

    with pytest.raises(ValueError, match="review slot is occupied"):
        R.submit_skill_update(**{**proposal, "challenger_body": "different candidate"})
    assert P.pending_path("pdf").read_text() == before


def test_concurrent_writer_cannot_be_displaced_between_check_and_publish(tmp_path, monkeypatch):
    root, _ = _library(tmp_path, monkeypatch)
    write_evidence = R._write_evidence

    def collide(skill, proposal, gate):
        paths = write_evidence(skill, proposal, gate)
        P.save_pending("pdf", {"skill": "pdf", "kind": "quality", "created": 7})
        return paths

    monkeypatch.setattr(R, "_write_evidence", collide)

    with pytest.raises(ValueError, match="review slot is occupied"):
        R.submit_skill_update(**_proposal(root))

    assert P.load_pending("pdf") == {"skill": "pdf", "kind": "quality", "created": 7}
    assert not list(R.evidence_dir().rglob("retrospective-*"))


@pytest.mark.parametrize(("change", "message"), [
    ({"champion_revision": "stale"}, "champion revision"),
    ({"skill": "missing"}, "no indexed skill"),
    ({"evidence": []}, "evidence"),
    ({"evidence": ["only one occurrence"]}, "at least two"),
    ({"evidence": ["same occurrence", "same occurrence"]}, "must be distinct"),
    ({"verification_status": "maybe"}, "verification_status must be passed"),
    ({"verification_status": "failed"}, "verification_status must be passed"),
    ({"verification_status": "unavailable"}, "verification_status must be passed"),
    ({"challenger_body": "x" * 200_001}, "challenger_body"),
])
def test_invalid_proposals_fail_before_mutation(tmp_path, monkeypatch, change, message):
    root, _ = _library(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match=message):
        R.submit_skill_update(**_proposal(root, **change))
    assert not P.pending_dir().exists()
    assert not R.evidence_dir().exists()


def test_atomic_publication_failure_cleans_evidence(tmp_path, monkeypatch):
    root, _ = _library(tmp_path, monkeypatch)

    def unsupported_link(_source, _destination):
        raise OSError("hard links unavailable")

    monkeypatch.setattr(R.os, "link", unsupported_link)
    with pytest.raises(RuntimeError, match="cannot atomically publish"):
        R.submit_skill_update(**_proposal(root))

    assert not P.pending_path("pdf").exists()
    assert not list(R.evidence_dir().rglob("retrospective-*"))
    assert not R.audit_file().exists()


def test_mcp_producer_reaches_existing_approval_and_rollback_path(tmp_path, monkeypatch):
    root, skill = _library(tmp_path, monkeypatch)
    from ingot.mcp_server import server
    server.STATE.reload([root])

    result = server.propose_skill_update(**_proposal(root))
    old_revision = _proposal(root)["champion_revision"]
    promoted = P._activate_approved("pdf", P.load_pending("pdf"), actor="retrospective-test")

    assert result["status"] == "quarantined"
    assert "Promoted 'pdf'" in promoted
    assert "new body with a reusable guard" in (skill / "SKILL.md").read_text()
    assert "old body" in (
        P.revisions_dir() / "pdf" / old_revision / "SKILL.md").read_text()

    P._activate_rollback("pdf", old_revision, actor="retrospective-test")
    assert "old body" in (skill / "SKILL.md").read_text()
