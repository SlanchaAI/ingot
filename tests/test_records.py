"""Candidate manifests and release receipts.

Two versioned records with one job each: a candidate names exactly what is being proposed and where
it came from; a release names exactly what was published and proves it happened. Both are consumed
by later PRs -- the candidate by `ingot add`, the receipt by the publisher -- so the shape is fixed
here and tested here."""
import json

import pytest

from ingot import records

REVISION = "a" * 64
OTHER_REVISION = "b" * 64


def _review(errors=(), warnings=()):
    return {"schema_version": "ingot/review/v1",
            "valid": not errors,
            "errors": list(errors),
            "warnings": list(warnings)}


def _candidate(**overrides):
    fields = {"kind": "creation",
              "skill": "pdf",
              "source_type": "file",
              "locator": "./packages/pdf",
              "resolved_revision": REVISION,
              "candidate_revision": REVISION,
              "review": _review(),
              "created_at": 1_770_000_000}
    fields.update(overrides)
    return records.candidate_manifest(**fields)


def _receipt(**overrides):
    fields = {"skill": "pdf",
              "action": "promote",
              "proposal_id": "p-1",
              "publication_id": "pub-1",
              "expected_champion": OTHER_REVISION,
              "candidate_revision": REVISION,
              "evidence_digests": [REVISION],
              "actor": "operator",
              "publisher": "local",
              "target": "managed-library",
              "published_at": 1_770_000_100,
              "result": "published"}
    fields.update(overrides)
    return records.release_receipt(**fields)


# --- digests --------------------------------------------------------------------------------

def test_digest_ignores_key_order():
    assert records.digest({"a": 1, "b": 2}) == records.digest({"b": 2, "a": 1})


def test_digest_changes_with_content():
    assert records.digest({"a": 1}) != records.digest({"a": 2})


def test_digest_is_a_sha256_hex():
    assert len(records.digest({"a": 1})) == 64


# --- candidate manifests --------------------------------------------------------------------

def test_candidate_manifest_carries_its_schema_version():
    assert _candidate()["schema_version"] == records.CANDIDATE_SCHEMA


def test_candidate_manifest_records_the_source_it_resolved():
    manifest = _candidate()

    assert manifest["source"] == {"type": "file",
                                  "locator": "./packages/pdf",
                                  "resolved_revision": REVISION}


def test_candidate_manifest_references_the_review_by_digest():
    """Included *and* digested: the report travels with the proposal, and the digest is what binds
    it, so a report edited after the fact stops matching."""
    review = _review(warnings=["file-reference-missing"])
    manifest = _candidate(review=review)

    assert manifest["review"]["digest"] == records.digest(review)
    assert manifest["review"]["warnings"] == ["file-reference-missing"]


def test_candidate_identity_ignores_the_timestamp():
    """Deterministic apart from timestamps and actor metadata: proposing the same bytes twice must
    produce the same identity, or idempotent submission is impossible."""
    first = _candidate(created_at=1_770_000_000)
    second = _candidate(created_at=1_999_999_999)

    assert records.candidate_identity(first) == records.candidate_identity(second)


def test_candidate_identity_ignores_the_local_path():
    """A local path is operator context. The same package submitted from two checkouts is the same
    candidate."""
    first = _candidate(locator="/home/a/pdf")
    second = _candidate(locator="/home/b/pdf")

    assert records.candidate_identity(first) == records.candidate_identity(second)


def test_file_candidate_identity_keeps_the_bound_review_report():
    """Break caught: changing identity for already-quarantined file candidates during upgrade."""
    first = _candidate(review={**_review(), "report_digest": REVISION})
    second = _candidate(review={**_review(), "report_digest": OTHER_REVISION})

    assert records.candidate_identity(first) != records.candidate_identity(second)


def test_candidate_identity_changes_with_the_candidate_revision():
    assert records.candidate_identity(_candidate()) != \
        records.candidate_identity(_candidate(candidate_revision=OTHER_REVISION))


def test_candidate_identity_changes_with_the_skill():
    assert records.candidate_identity(_candidate()) != \
        records.candidate_identity(_candidate(skill="docx"))


def test_candidate_identity_changes_with_the_review_outcome():
    """Evidence is revision-bound, and a review is evidence. The same bytes reviewed clean and
    reviewed with errors are not interchangeable proposals."""
    assert records.candidate_identity(_candidate()) != \
        records.candidate_identity(_candidate(review=_review(errors=["description-empty"])))


def test_a_well_formed_candidate_validates():
    assert records.validate_candidate(_candidate()) == []


def test_a_candidate_with_the_wrong_schema_is_rejected():
    manifest = _candidate()
    manifest["schema_version"] = "ingot/candidate/v99"

    assert any("schema" in problem for problem in records.validate_candidate(manifest))


def test_a_candidate_missing_a_field_is_rejected():
    manifest = _candidate()
    del manifest["candidate_revision"]

    assert any("candidate_revision" in problem for problem in records.validate_candidate(manifest))


def test_a_candidate_with_a_semantic_version_source_is_rejected():
    """The resolved source revision must be content-based. A tag can be moved; a digest cannot."""
    problems = records.validate_candidate(_candidate(resolved_revision="v1.2.3"))

    assert any("resolved_revision" in problem for problem in problems)


def test_a_candidate_with_an_unknown_kind_is_rejected():
    assert any("kind" in problem for problem in records.validate_candidate(_candidate(kind="mutate")))


def test_a_candidate_with_an_invalid_skill_slug_is_rejected():
    assert any("skill" in problem for problem in records.validate_candidate(_candidate(skill="Not_A_Slug")))


# --- release receipts -----------------------------------------------------------------------

def test_release_receipt_carries_its_schema_version():
    assert _receipt()["schema_version"] == records.RELEASE_SCHEMA


def test_a_well_formed_receipt_validates():
    assert records.validate_release(_receipt()) == []


def test_absence_is_a_valid_revision_on_both_sides():
    """A creation displaces nothing and a rollback can restore nothing. Absence is a revision, and
    the existing publisher already treats it as one."""
    assert records.validate_release(_receipt(expected_champion=records.ABSENT_REVISION)) == []
    assert records.validate_release(_receipt(action="rollback",
                                             candidate_revision=records.ABSENT_REVISION)) == []


def test_a_rollback_produces_a_receipt():
    assert records.validate_release(_receipt(action="rollback")) == []


def test_an_unknown_action_is_rejected():
    assert any("action" in problem for problem in records.validate_release(_receipt(action="delete")))


def test_a_failed_receipt_stays_inspectable():
    """A failure that erased its own reason would leave an operator with a stalled lane and no
    way to learn why."""
    receipt = records.release_receipt(
        skill="pdf", action="promote", proposal_id="p-1", publication_id="pub-1",
        expected_champion=OTHER_REVISION, candidate_revision=REVISION, evidence_digests=[],
        actor="operator", publisher="local", target="managed-library",
        published_at=1_770_000_100, result="failed", error="vault champion did not match")

    assert records.validate_release(receipt) == []
    assert receipt["result"] == "failed"
    assert receipt["error"] == "vault champion did not match"


def test_a_published_receipt_may_not_carry_an_error():
    receipt = _receipt()
    receipt["error"] = "something went wrong"

    assert any("error" in problem for problem in records.validate_release(receipt))


def test_a_failed_receipt_must_say_why():
    receipt = _receipt(result="failed")

    assert any("error" in problem for problem in records.validate_release(receipt))


def test_an_unknown_result_is_rejected():
    assert any("result" in problem for problem in records.validate_release(_receipt(result="queued")))


def test_a_receipt_is_not_signed_and_claims_nothing_about_tampering():
    """No signature field, on purpose. A local record a machine administrator can rewrite must not
    carry anything that looks like proof it was not."""
    receipt = _receipt()

    assert "signature" not in receipt
    assert "attestation" not in receipt


# --- round trips ----------------------------------------------------------------------------

def test_both_records_survive_a_json_round_trip():
    for record in (_candidate(), _receipt()):
        assert json.loads(json.dumps(record)) == record


def test_records_import_without_the_heavy_stack():
    import subprocess
    import sys

    heavy = ["fastapi", "onnxruntime", "langgraph", "langfuse", "litellm", "fastembed"]
    program = f"import sys, ingot.records; print([m for m in {heavy!r} if m in sys.modules])"

    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
