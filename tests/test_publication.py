import json
import stat
from pathlib import Path

import pytest

from ingot.optimize import publication as Q


@pytest.fixture(autouse=True)
def publication_store(tmp_path, monkeypatch):
    monkeypatch.setenv("INGOT_RUNS", str(tmp_path))


def _pending(*, candidate="b" * 64, body="new body", action="promote"):
    return {
        "skill": "copywriting",
        "kind": "retrospective",
        "champion_components": {"description": "Write copy.", "body": "old body"},
        "challenger_components": (
            {} if candidate == "absent" else {"description": "Write copy.", "body": body}
        ),
        "evidence": {
            "champion": {"revision": "a" * 64},
            "challenger": {"revision": candidate},
            "gate": {"promotable": True, "blocked": []},
        },
        "retrospective": {"proposal_id": "retro-123"},
    }


def test_queue_is_inert_and_captures_exact_approval(tmp_path, monkeypatch):
    library = tmp_path / "skills"
    monkeypatch.setenv("INGOT_LIBRARY", str(library))
    pending = _pending()

    receipt = Q.queue_publication("copywriting", pending, "admin", "promote")

    assert receipt.state == "approved_publishing"
    assert not (library / "copywriting").exists()
    stored = json.loads(receipt.path.read_text())
    assert stored["proposal_id"] == "retro-123"
    assert stored["actor"] == "admin"
    assert stored["action"] == "promote"
    assert stored["expected_champion"] == "a" * 64
    assert stored["candidate_revision"] == "b" * 64
    assert stored["components"] == pending["challenger_components"]
    assert stored["attempts"] == 0 and stored["last_error"] == ""
    assert stat.S_IMODE(receipt.path.stat().st_mode) == 0o600


def test_exact_retry_is_idempotent():
    first = Q.queue_publication("copywriting", _pending(), "admin", "promote")
    second = Q.queue_publication("copywriting", _pending(), "admin", "promote")

    assert second.id == first.id
    assert list(Q.publications_dir().glob("*.json")) == [first.path]


def test_different_candidate_cannot_occupy_same_skill_lane():
    Q.queue_publication("copywriting", _pending(), "admin", "promote")

    with pytest.raises(ValueError, match="publication is already in progress"):
        Q.queue_publication(
            "copywriting", _pending(candidate="c" * 64, body="other body"),
            "admin", "promote",
        )


def test_final_record_is_absent_when_atomic_publication_fails(monkeypatch):
    def fail_link(source, destination):
        raise OSError("disk failure")

    monkeypatch.setattr(Q.os, "link", fail_link)
    with pytest.raises(OSError, match="disk failure"):
        Q.queue_publication("copywriting", _pending(), "admin", "promote")

    assert list(Q.publications_dir().glob("*.json")) == []


def test_absence_rollback_can_queue_without_skill_components():
    receipt = Q.queue_publication(
        "copywriting", _pending(candidate="absent"), "admin", "rollback"
    )

    stored = json.loads(receipt.path.read_text())
    assert stored["action"] == "rollback"
    assert stored["candidate_revision"] == "absent"
    assert stored["components"] == {}


def test_the_newest_receipt_wins_within_one_second(tmp_path, monkeypatch):
    """Two receipts for one skill are routinely queued in the same second — approve, then roll
    back. Whole-second timestamps would leave the review surface showing whichever id sorted
    higher."""
    monkeypatch.setenv("INGOT_LIBRARY", str(tmp_path / "skills"))
    first = Q.queue_publication("copywriting", _pending(), "admin", "promote")
    Q.update_publication(first.id, state="active")
    rollback = _pending(candidate="absent")
    second = Q.queue_publication("copywriting", rollback, "admin", "rollback")

    latest = Q.publication_for_skill("copywriting")

    assert latest["id"] == second.id and latest["id"] != first.id
    assert latest["created"] == json.loads(first.path.read_text())["created"]  # the same second


@pytest.mark.parametrize("malformed", [[], "", 0])
def test_rollback_refuses_components_of_the_wrong_shape(malformed):
    """Absence is expressed by an empty object, not by any falsy value a malformed record holds."""
    pending = _pending(candidate="absent")
    pending["challenger_components"] = malformed

    with pytest.raises(ValueError, match="components must be an object"):
        Q.queue_publication("copywriting", pending, "admin", "rollback")


def test_promotion_still_requires_description_and_body():
    with pytest.raises(ValueError, match="challenger components are required"):
        Q.queue_publication(
            "copywriting", _pending(candidate="absent"), "admin", "promote"
        )
