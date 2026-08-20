"""`ingot pending`, `approve`, `reject`, `history`, `rollback`.

The command line wraps the services the console already calls. What these tests protect is that it
stays a wrapper: no second approval path, no direct activation helper, and no verb that changes a
served byte. Approval and rollback queue a receipt; the publisher is what acts on it."""
import json

import pytest

from ingot import cli, decisions
from ingot.mcp_server.registry import optimizable_components, skill_revision
from ingot.optimize import promote as P
from ingot.optimize import publication as Q


def _library(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv("INGOT_LIBRARY", str(root))
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(root))
    return root


def _skill(root, name="pdf", body="old body"):
    directory = root / name
    directory.mkdir(exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Merge PDFs.\n---\n{body}\n")
    return directory


def _quarantine(directory, *, promotable=True, blocked=()):
    skill = directory.name
    champion = optimizable_components(directory)
    challenger = {**champion, "body": "new body"}
    pending = {
        "skill": skill,
        "champion_components": champion,
        "challenger_components": challenger,
        "gate": {"promotable": promotable, "blocked": list(blocked)},
        "evidence": {"champion": {"revision": skill_revision(directory)},
                     "challenger": {"revision": skill_revision(directory, challenger)}},
    }
    P.save_pending(skill, pending)
    return pending


# --------------------------------------------------------------------------- the four words

@pytest.mark.parametrize("record,expected", [
    ({"state": "approved_publishing"}, decisions.APPROVED),
    ({"state": "publishing"}, decisions.PUBLISHING),
    ({"state": "awaiting_merge"}, decisions.PUBLISHING),
    ({"state": "active"}, decisions.PUBLISHED),
    ({"state": "publishing", "last_error": "git push: rejected"}, decisions.FAILED),
    ({"state": "approved_publishing", "last_error": "vault is dirty"}, decisions.FAILED),
])
def test_a_receipt_reports_the_word_an_operator_has_to_act_on(record, expected):
    """`failed` reads off `last_error`, not the state: a failed attempt leaves the receipt in
    whatever state it was working through, so a status taken from the state alone hides it."""
    assert decisions.release_status(record)["status"] == expected


def test_an_active_receipt_that_once_failed_reads_as_published():
    """A receipt that recovered is published. `last_error` is cleared on success, and reporting a
    stale error over a completed activation would send someone chasing a resolved failure."""
    assert decisions.release_status(
        {"state": "active", "last_error": ""})["status"] == decisions.PUBLISHED


def test_no_receipt_is_not_a_status():
    assert decisions.release_status(None) is None


# --------------------------------------------------------------------------- pending

def test_pending_lists_what_is_waiting_with_its_gate_verdict(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    _quarantine(_skill(root, "pdf"))
    _quarantine(_skill(root, "tailwind"), promotable=False, blocked=["holdout regression"])

    result = decisions.pending_view()

    assert [entry["skill"] for entry in result["pending"]] == ["pdf", "tailwind"]
    assert result["pending"][0]["promotable"] is True
    assert result["pending"][1]["promotable"] is False
    assert result["pending"][1]["blocked"] == ["holdout regression"]


def test_pending_still_shows_a_change_after_approval_consumes_its_record(tmp_path, monkeypatch):
    """Approval consumes the pending record at publication, so the window in which someone is most
    likely to ask where their change went is exactly the window the queue cannot see."""
    root = _library(tmp_path, monkeypatch)
    _quarantine(_skill(root))
    decisions.approve("pdf", actor="admin")
    P.pending_path("pdf").unlink()

    result = decisions.pending_view()

    assert result["pending"] == []
    assert [entry["skill"] for entry in result["publishing"]] == ["pdf"]
    assert result["publishing"][0]["status"] == decisions.APPROVED


def test_pending_reports_an_empty_queue_rather_than_nothing(tmp_path, monkeypatch, capsys):
    _library(tmp_path, monkeypatch)

    assert cli.main(["pending"]) == 0
    assert "Nothing waiting." in capsys.readouterr().out


# --------------------------------------------------------------------------- decisions

def test_approve_queues_a_receipt_and_changes_no_served_byte(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    directory = _skill(root)
    _quarantine(directory)
    before = (directory / "SKILL.md").read_bytes()

    result = decisions.approve("pdf", actor="admin")

    assert result["publication"]["status"] == decisions.APPROVED
    assert result["publication"]["action"] == "promote"
    assert (directory / "SKILL.md").read_bytes() == before
    assert Q.load_publication(result["publication"]["id"])["state"] == "approved_publishing"


def test_approve_refuses_a_change_the_evidence_gate_blocked(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    _quarantine(_skill(root), promotable=False, blocked=["holdout regression"])

    with pytest.raises(ValueError, match="evidence gate blocked"):
        decisions.approve("pdf", actor="admin")

    assert Q.publication_for_skill("pdf") is None


def test_approve_refuses_a_second_publication_for_the_same_skill(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    _quarantine(_skill(root))
    P._snapshot_absence("pdf")
    decisions.approve("pdf", actor="admin")

    with pytest.raises(ValueError, match="already in progress"):
        decisions.rollback("pdf", P.ABSENT_REVISION, actor="admin")


def test_reject_discards_the_change_and_records_the_reason(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    directory = _skill(root)
    _quarantine(directory)
    before = (directory / "SKILL.md").read_bytes()

    result = decisions.reject("pdf", actor="admin", reason="  the  body  loses  the  API  note  ")

    assert not P.pending_path("pdf").exists()
    assert (directory / "SKILL.md").read_bytes() == before
    assert result["publication"] is None
    record = P.read_audit()["records"][0]
    assert record["action"] == "reject" and record["actor"] == "admin"
    assert record["reason"] == "the body loses the API note"


def test_reject_without_a_pending_change_is_a_refusal_not_a_crash(tmp_path, monkeypatch):
    _library(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="no pending change"):
        decisions.reject("pdf", actor="admin")


def test_rollback_queues_the_snapshot_without_restoring_it(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    directory = _skill(root)
    champion = skill_revision(directory)
    P._snapshot(directory, "pdf", champion)
    _skill(root, body="a later revision")

    result = decisions.rollback("pdf", champion, actor="admin")

    assert result["publication"]["action"] == "rollback"
    assert result["publication"]["revision"] == champion
    assert "a later revision" in (directory / "SKILL.md").read_text()


def test_rollback_to_a_revision_with_no_snapshot_is_refused(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    _skill(root)

    with pytest.raises(ValueError, match="no snapshot"):
        decisions.rollback("pdf", "f" * 64, actor="admin")


# --------------------------------------------------------------------------- history

def test_history_gathers_snapshots_receipts_and_decisions_for_one_skill(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    directory = _skill(root)
    champion = skill_revision(directory)
    P._snapshot(directory, "pdf", champion)
    _quarantine(directory)
    decisions.approve("pdf", actor="admin")
    _quarantine(_skill(root, "tailwind"))
    decisions.reject("tailwind", actor="admin", reason="not this one")

    result = decisions.history_view("pdf")

    assert [entry["revision"] for entry in result["revisions"]] == [champion]
    assert [entry["status"] for entry in result["publications"]] == [decisions.APPROVED]
    assert [record["skill"] for record in result["audit"]] == []   # approval audits on activation


def test_history_never_reports_another_skills_decisions(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    _quarantine(_skill(root, "tailwind"))
    decisions.reject("tailwind", actor="admin", reason="not this one")

    assert decisions.history_view("pdf")["audit"] == []
    assert decisions.history_view("tailwind")["audit"][0]["reason"] == "not this one"


def test_history_refuses_an_invalid_skill_name(tmp_path, monkeypatch):
    _library(tmp_path, monkeypatch)

    with pytest.raises(ValueError):
        decisions.history_view("../etc")


# --------------------------------------------------------------------------- the command line

def test_the_command_line_reports_the_receipt_and_that_nothing_is_served_yet(tmp_path, monkeypatch,
                                                                            capsys):
    root = _library(tmp_path, monkeypatch)
    _quarantine(_skill(root))

    assert cli.main(["approve", "pdf", "--actor", "admin"]) == 0

    out = capsys.readouterr().out
    assert "Approved 'pdf'" in out
    assert "approved" in out
    assert "The served library is unchanged" in out


def test_a_refusal_exits_one_without_a_traceback(tmp_path, monkeypatch, capsys):
    _library(tmp_path, monkeypatch)

    assert cli.main(["approve", "pdf"]) == 1
    assert "ingot approve: no pending challenger" in capsys.readouterr().err


def test_every_decision_payload_is_versioned(tmp_path, monkeypatch, capsys):
    root = _library(tmp_path, monkeypatch)
    _quarantine(_skill(root))

    assert cli.main(["approve", "pdf", "--actor", "admin", "--json"]) == 0

    assert json.loads(capsys.readouterr().out)["schema_version"] == decisions.DECISION_SCHEMA


def test_an_explicit_actor_outranks_the_environment(tmp_path, monkeypatch):
    """A decision nobody can be asked about is not much of an audit trail, so the actor is never
    blank: an explicit flag, then INGOT_ACTOR, then whoever is running the command."""
    root = _library(tmp_path, monkeypatch)
    _quarantine(_skill(root, "pdf"))
    _quarantine(_skill(root, "tailwind"))
    monkeypatch.setenv("INGOT_ACTOR", "from-the-environment")

    assert cli.main(["reject", "pdf", "--actor", "reviewer", "--reason", "no"]) == 0
    assert cli.main(["reject", "tailwind", "--reason", "no"]) == 0

    actors = {record["skill"]: record["actor"] for record in P.read_audit()["records"]}
    assert actors == {"pdf": "reviewer", "tailwind": "from-the-environment"}
