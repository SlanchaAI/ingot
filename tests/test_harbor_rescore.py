"""Regression tests for Harbor evidence-only rescoring.

These fixtures contain completed Harbor-style trials but replace only the external judge.  The
rescorer still reads the same on-disk result and verifier artifact layout that live runs retain.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from langfuse import Langfuse

from ingot.optimize import agy_judge as A
from ingot.optimize import harbor_langfuse as L
from ingot.optimize import harbor_report
from ingot.optimize import harbor_rescore as R


FINGERPRINT = "tasks-v1"
HISTORICAL_JUDGE = "judge-a"
HISTORICAL_REVISION = "harbor-rubric-v1"
RUNTIME = "agy 1.1.11"
SKILL_BODY = "fixture skill body\n"
MIGRATION_PROVENANCE = {
    "skill": "demo",
    "skill_body": SKILL_BODY,
    "skill_sha256": hashlib.sha256(SKILL_BODY.encode()).hexdigest(),
    "task_texts": {"demo-h0": "Use deterministic fixture evidence."},
}


def metadata(combination: str, *, fingerprint: str = "endpoint-a", attempts: int = 3) -> dict:
    return {
        "combination": combination,
        "harness": "codex",
        "model": "qwen3.6-27b",
        "target_alias": "dell",
        "endpoint_fingerprint": fingerprint,
        "protocol": "openai",
        "task_fingerprint": FINGERPRINT,
        "attempts": attempts,
        "judge": HISTORICAL_JUDGE,
        "scoring_revision": HISTORICAL_REVISION,
        **MIGRATION_PROVENANCE,
    }


def write_combo(root: Path, name: str, record: dict, *, artifact: str = "answer",
                recorded_attempts: int = 3, receipt_metadata: dict | None = None) -> Path:
    """Write a completed two-arm Harbor combination with recorded attempts per arm."""
    combo = root / name
    combo.mkdir(parents=True)
    (combo / "combo.json").write_text(json.dumps(record))
    for arm in ("skill", "control"):
        for attempt in range(1, recorded_attempts + 1):
            solution = combo / arm / f"demo-h0__attempt-{attempt}" / "verifier" / "solution"
            solution.mkdir(parents=True)
            (solution / "answer.md").write_text(f"{arm} {artifact}")
            trial = solution.parent.parent
            (trial / "result.json").write_text(json.dumps({
                "task_name": f"ingot/demo-h0__attempt-{attempt}", "exception_info": {},
            }))
            payload = L.build_attempt_payload(
                trial, {**(receipt_metadata or record), "arm": arm})
            digest = L._payload_sha256(payload)
            (trial / "langfuse-receipt.json").write_text(json.dumps({
                "status": "verified",
                "trace_id": Langfuse.create_trace_id(
                    seed=f"{L.EXPORTER_REVISION}:{digest}"),
                "payload_sha256": digest,
                "exporter_revision": L.EXPORTER_REVISION,
            }))
    return combo


def patch_current_facts(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    holdout = [{"task": "demo", "rubric": ""}]
    monkeypatch.setenv("JUDGE_BACKEND", "agy")
    monkeypatch.setattr(R, "_task_fingerprint", lambda tasks: FINGERPRINT)
    monkeypatch.setattr(R, "preflight", lambda: {
        "identity": R.AGY_IDENTITY,
        "model": "gemini-3.6-flash-medium",
        "version": RUNTIME,
        "billing_mode": "subscription",
    })
    return holdout


def write_legacy_manifest(path: Path, root: Path, *, attempts: int = 3) -> Path:
    path.write_text(json.dumps({
        "root": str(root),
        "task_fingerprint": FINGERPRINT,
        "attempts": attempts,
    }))
    return path


def write_provenance(path: Path) -> Path:
    path.write_text(json.dumps(MIGRATION_PROVENANCE))
    return path


def patch_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    holdout = patch_current_facts(monkeypatch)
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], holdout, {}))

    def fake_score(answers, *_args):
        return [0.8 if "skill answer" in "\n".join(sum(answers.values(), [])) else 0.4]

    monkeypatch.setattr(R, "score", fake_score)


def test_discovery_uses_complete_combo_identity_across_preserved_and_local_roots(tmp_path):
    preserved = tmp_path / "jobs" / "demo"
    local = tmp_path / "jobs" / "demo-k3"
    old = {"combination": "codex@qwen3.6-27b", "harness": "codex", "model": "qwen3.6-27b"}
    local_record = metadata("codex@qwen3.6-27b--dell-22222222", fingerprint="2" * 64)
    old_combo = write_combo(preserved, "codex_qwen3.6-27b", old)
    local_combo = write_combo(local, "codex@qwen3.6-27b--dell-22222222", local_record)

    assert R.discover_combinations([preserved, local]) == [old_combo, local_combo]
    assert R._combo_identity(local_combo) == local_record


@pytest.mark.parametrize("field, value", [
    ("task_fingerprint", "other-tasks"),
    ("attempts", 1),
])
def test_compatibility_mismatch_stops_before_scoring_or_publication(tmp_path, monkeypatch, field, value):
    root = tmp_path / "jobs"
    first = metadata("codex@qwen--dell-a", fingerprint="a" * 64)
    second = metadata("codex@qwen--dell-b", fingerprint="b" * 64)
    second[field] = value
    write_combo(root, "first", first)
    write_combo(root, "second", second)
    out = tmp_path / "demo.rescored.json"
    out.write_bytes(b"known-good")
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    holdout = patch_current_facts(monkeypatch)
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], holdout, {}))
    monkeypatch.setattr(R, "score", lambda *_args: pytest.fail("scoring must not start"))

    with pytest.raises(ValueError, match=field):
        R.rescore("demo", jobs_roots=[root], log=lambda *_args: None)

    assert out.read_bytes() == b"known-good"


def test_legacy_proprietary_combo_uses_explicit_manifest_with_the_real_matrix_shape(tmp_path, monkeypatch):
    harbor = tmp_path / "harbor"
    root = harbor / "jobs" / "demo"
    legacy = {"combination": "codex@qwen", "harness": "codex", "model": "qwen"}
    write_combo(root, "codex_qwen", legacy,
                receipt_metadata={**legacy, **MIGRATION_PROVENANCE})
    (harbor / "demo.json").write_text(json.dumps({
        "skill": "demo",
        "tasks": 1,
        "pinned_model": None,
        "judge": HISTORICAL_JUDGE,
        "harnesses": {"codex@qwen": {"attempts": 3}},
    }))
    manifest = write_legacy_manifest(tmp_path / "legacy.json", root)
    provenance = write_provenance(tmp_path / "provenance.json")
    monkeypatch.setattr(R, "HARBOR_DIR", harbor)
    patch_scoring(monkeypatch)

    summary = R.rescore(
        "demo", jobs_roots=[root], legacy_metadata=manifest,
        provenance_metadata=[provenance], log=lambda *_args: None)

    row = summary["combinations"]["codex@qwen"]
    assert row["attempts"] == 3
    assert row["judge"] == R.AGY_IDENTITY
    assert row["task_fingerprint"] == FINGERPRINT
    assert row["scoring_revision"] == "harbor-rubric-v2-agy"
    assert row["judge_runtime"] == RUNTIME
    assert row["judge_billing_mode"] == "subscription"


def test_legacy_manifest_authorizes_its_exact_nondefault_preserved_root(tmp_path, monkeypatch):
    harbor = tmp_path / "harbor"
    root = harbor / "jobs" / "demo-k3"
    legacy = {"combination": "codex@qwen", "harness": "codex", "model": "qwen"}
    write_combo(root, "codex_qwen", legacy,
                receipt_metadata={**legacy, **MIGRATION_PROVENANCE})
    (harbor / "demo.json").write_text(json.dumps({
        "skill": "demo",
        "tasks": 1,
        "pinned_model": None,
        "judge": HISTORICAL_JUDGE,
        "harnesses": {"codex@qwen": {"attempts": 3}},
    }))
    manifest = write_legacy_manifest(tmp_path / "legacy.json", root)
    provenance = write_provenance(tmp_path / "provenance.json")
    monkeypatch.setattr(R, "HARBOR_DIR", harbor)
    patch_scoring(monkeypatch)

    summary = R.rescore(
        "demo", jobs_roots=[root], legacy_metadata=manifest,
        provenance_metadata=[provenance], log=lambda *_args: None)

    assert summary["combinations"]["codex@qwen"]["attempts"] == 3


def test_legacy_combo_without_explicit_manifest_is_refused(tmp_path, monkeypatch):
    harbor = tmp_path / "harbor"
    root = harbor / "jobs" / "demo"
    write_combo(root, "codex_qwen", {"combination": "codex@qwen", "harness": "codex", "model": "qwen"})
    (harbor / "demo.json").write_text(json.dumps({
        "skill": "demo", "tasks": 1, "pinned_model": None, "judge": HISTORICAL_JUDGE,
        "harnesses": {"codex@qwen": {"attempts": 3}},
    }))
    monkeypatch.setattr(R, "HARBOR_DIR", harbor)
    holdout = patch_current_facts(monkeypatch)
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], holdout, {}))
    monkeypatch.setattr(R, "score", lambda *_args: pytest.fail("scoring must not start"))

    with pytest.raises(ValueError, match="manifest"):
        R.rescore("demo", jobs_roots=[root], log=lambda *_args: None)


def test_legacy_combo_refuses_matrix_metadata_that_conflicts_with_its_record(tmp_path, monkeypatch):
    harbor = tmp_path / "harbor"
    root = harbor / "jobs" / "demo"
    legacy = {"combination": "codex@qwen", "harness": "codex", "model": "qwen", "attempts": 1}
    write_combo(root, "codex_qwen", legacy)
    (harbor / "demo.json").write_text(json.dumps({
        "skill": "demo", "tasks": 1, "pinned_model": None,
        "judge": HISTORICAL_JUDGE,
        "harnesses": {"codex@qwen": {"attempts": 3}},
    }))
    manifest = write_legacy_manifest(tmp_path / "legacy.json", root)
    monkeypatch.setattr(R, "HARBOR_DIR", harbor)
    holdout = patch_current_facts(monkeypatch)
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], holdout, {}))
    monkeypatch.setattr(R, "score", lambda *_args: pytest.fail("scoring must not start"))

    with pytest.raises(ValueError, match="attempts"):
        R.rescore("demo", jobs_roots=[root], legacy_metadata=manifest, log=lambda *_args: None)


def test_legacy_combo_refuses_manifest_for_the_wrong_root(tmp_path, monkeypatch):
    harbor = tmp_path / "harbor"
    root = harbor / "jobs" / "demo"
    legacy = {"combination": "codex@qwen", "harness": "codex", "model": "qwen"}
    write_combo(root, "codex_qwen", legacy)
    (harbor / "demo.json").write_text(json.dumps({
        "skill": "demo", "tasks": 1, "pinned_model": None,
        "judge": HISTORICAL_JUDGE,
        "harnesses": {"codex@qwen": {"attempts": 3}},
    }))
    manifest = write_legacy_manifest(tmp_path / "legacy.json", harbor / "jobs" / "other")
    monkeypatch.setattr(R, "HARBOR_DIR", harbor)
    holdout = patch_current_facts(monkeypatch)
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], holdout, {}))
    monkeypatch.setattr(R, "score", lambda *_args: pytest.fail("scoring must not start"))

    with pytest.raises(ValueError, match="root"):
        R.rescore("demo", jobs_roots=[root], legacy_metadata=manifest, log=lambda *_args: None)


@pytest.mark.parametrize("field, value", [
    ("attempts", 0),
    ("attempts", False),
    ("task_fingerprint", False),
])
def test_compatibility_rejects_invalid_shared_metadata(field, value):
    record = metadata("codex@qwen--dell")
    record[field] = value

    with pytest.raises(ValueError, match=field):
        R.validate_compatibility([record])


def test_public_compatibility_validator_accepts_one_argument_and_only_checks_records():
    record = metadata("codex@qwen--dell")

    R.validate_compatibility([record])


@pytest.mark.parametrize("field, value", [
    ("task_fingerprint", "old-tasks"),
    ("attempts", 1),
])
def test_current_fact_mismatch_stops_before_scoring_or_publication(tmp_path, monkeypatch, field, value):
    root = tmp_path / "jobs"
    first = metadata("codex@qwen--dell-a", fingerprint="a" * 64)
    second = metadata("codex@qwen--dell-b", fingerprint="b" * 64)
    first[field] = second[field] = value
    write_combo(root, "first", first)
    write_combo(root, "second", second)
    out = tmp_path / "demo.rescored.json"
    out.write_bytes(b"known-good")
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    holdout = patch_current_facts(monkeypatch)
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], holdout, {}))
    monkeypatch.setattr(R, "score", lambda *_args: pytest.fail("scoring must not start"))

    with pytest.raises(ValueError, match=field):
        R.rescore("demo", jobs_roots=[root], log=lambda *_args: None)

    assert out.read_bytes() == b"known-good"


def test_rescore_refuses_missing_attempt_receipt_before_preflight_judge_or_publication(tmp_path, monkeypatch):
    """Removing the live receipt gate would silently publish a matrix with withheld telemetry."""
    root = tmp_path / "jobs"
    combo = write_combo(root, "one", metadata("codex@qwen--dell", fingerprint="a" * 64))
    missing = next(combo.glob("skill/*/langfuse-receipt.json"))
    missing.unlink()
    out = tmp_path / "demo.rescored.json"
    out.write_bytes(b"known-good")
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    monkeypatch.setenv("JUDGE_BACKEND", "agy")
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], [{"task": "demo", "rubric": ""}], {}))
    monkeypatch.setattr(R, "_task_fingerprint", lambda tasks: FINGERPRINT)
    monkeypatch.setattr(R, "preflight", lambda: pytest.fail("judge preflight must not start"))
    monkeypatch.setattr(R, "score", lambda *_args: pytest.fail("judge must not start"))

    with pytest.raises(L.TelemetryReceiptError, match="receipt"):
        R.rescore("demo", jobs_roots=[root], log=lambda *_args: None)

    assert out.read_bytes() == b"known-good"


def test_one_persisted_provenance_document_authorizes_export_and_rescore(tmp_path, monkeypatch):
    """The same explicit migration document must authorize preserved export and publication."""
    root = tmp_path / "jobs"
    record = metadata("codex@qwen--dell", fingerprint="a" * 64)
    for field in MIGRATION_PROVENANCE:
        record.pop(field)
    combo = write_combo(
        root, "one", record, receipt_metadata={**record, **MIGRATION_PROVENANCE})
    for receipt in combo.glob("*/*/langfuse-receipt.json"):
        receipt.unlink()
    provenance_path = write_provenance(tmp_path / "provenance.json")

    class Observation:
        def __init__(self, identifier: str) -> None:
            self.id = identifier

        def end(self) -> None:
            pass

    class PersistedReadback:
        def __init__(self) -> None:
            self.observations = {}

        def create_trace_id(self, *, seed: str) -> str:
            return Langfuse.create_trace_id(seed=seed)

        def start_observation(self, **kwargs):
            identifier = f"{len(self.observations) + 1:016x}"
            trace_id = kwargs["trace_context"]["trace_id"]
            self.observations[trace_id] = {
                "id": identifier,
                "name": kwargs["name"],
                "type": kwargs["as_type"].upper(),
                "metadata": kwargs["metadata"],
            }
            return Observation(identifier)

        def flush(self) -> None:
            pass

        def read_trace(self, trace_id: str):
            observation = self.observations.get(trace_id)
            return {"id": trace_id, "observations": [observation]} if observation else None

    client = PersistedReadback()
    provenance = L.load_provenance_metadata(provenance_path)
    assert len(L.export_evidence_root(root, metadata=provenance, client=client)) == 6
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    patch_scoring(monkeypatch)

    summary = R.rescore(
        "demo", jobs_roots=[root], provenance_metadata=[provenance_path],
        log=lambda *_args: None)

    assert summary["scored"] == 1
    assert len(client.observations) == 6


def test_rescore_rejects_provenance_free_receipts_before_judge_preflight(tmp_path, monkeypatch):
    """Even exact v2 receipts cannot authorize publication without explicit provenance."""
    root = tmp_path / "jobs"
    record = metadata("codex@qwen--dell", fingerprint="a" * 64)
    for field in MIGRATION_PROVENANCE:
        record.pop(field)
    write_combo(root, "one", record)
    out = tmp_path / "demo.rescored.json"
    out.write_bytes(b"known-good")
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    monkeypatch.setenv("JUDGE_BACKEND", "agy")
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], [{"task": "demo", "rubric": ""}], {}))
    monkeypatch.setattr(R, "_task_fingerprint", lambda tasks: FINGERPRINT)
    monkeypatch.setattr(R, "preflight", lambda: pytest.fail("judge preflight must not start"))

    with pytest.raises(L.TelemetryReceiptError, match="provenance"):
        R.rescore("demo", jobs_roots=[root], log=lambda *_args: None)

    assert out.read_bytes() == b"known-good"


def test_rescore_refuses_non_agy_backend_before_preflight_or_scoring(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    write_combo(root, "one", metadata("codex@qwen--dell", fingerprint="a" * 64))
    out = tmp_path / "demo.rescored.json"
    out.write_bytes(b"known-good")
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    holdout = patch_current_facts(monkeypatch)
    monkeypatch.setenv("JUDGE_BACKEND", "openrouter")
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], holdout, {}))
    monkeypatch.setattr(R, "preflight", lambda: pytest.fail("Agy preflight ran under wrong backend"))
    monkeypatch.setattr(R, "score", lambda *_args: pytest.fail("scoring ran under wrong backend"))

    with pytest.raises(RuntimeError, match="JUDGE_BACKEND=agy"):
        R.rescore("demo", jobs_roots=[root], log=lambda *_args: None)

    assert out.read_bytes() == b"known-good"


def test_historical_scorers_do_not_block_one_current_agy_rescore(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    first = metadata("codex@qwen--dell-a", fingerprint="a" * 64)
    second = metadata("codex@qwen--dell-b", fingerprint="b" * 64)
    second.update({
        "judge": "other/historical-judge",
        "scoring_revision": "harbor-rubric-v0",
        "judge_runtime": "old runtime",
        "judge_billing_mode": "metered",
        "cost_usd": 1.23,
    })
    write_combo(root, "first", first)
    write_combo(root, "second", second)
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    holdout = patch_current_facts(monkeypatch)
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], holdout, {}))
    preflights = []
    monkeypatch.setattr(R, "preflight", lambda: preflights.append(True) or {
        "identity": R.AGY_IDENTITY,
        "model": "gemini-3.6-flash-medium",
        "version": RUNTIME,
        "billing_mode": "subscription",
    })
    score_calls = []

    def fake_score(answers, *_args):
        score_calls.append(answers)
        return [0.8 if "skill answer" in "\n".join(sum(answers.values(), [])) else 0.4]

    monkeypatch.setattr(R, "score", fake_score)

    summary = R.rescore("demo", jobs_roots=[root], log=lambda *_args: None)

    assert preflights == [True]
    assert len(score_calls) == 4
    assert summary["judge"] == R.AGY_IDENTITY
    assert summary["scoring_revision"] == "harbor-rubric-v2-agy"
    assert summary["judge_runtime"] == RUNTIME
    assert summary["judge_billing_mode"] == "subscription"
    for row in summary["combinations"].values():
        assert row["judge"] == R.AGY_IDENTITY
        assert row["scoring_revision"] == "harbor-rubric-v2-agy"
        assert row["judge_runtime"] == RUNTIME
        assert row["judge_billing_mode"] == "subscription"
        assert "cost_usd" not in row
    assert "cost_usd" not in summary


def test_stale_measurements_cannot_make_an_agy_failure_rankable(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    stale = {
        "error": "historical failure",
        "score": 0.9,
        "scores": [0.9],
        "skill_mean": 0.9,
        "control_mean": 0.1,
        "lift": 0.8,
        "skill_scores": [0.9],
        "control_scores": [0.1],
        "tasks_scored": 99,
        "tasks_dropped": ["old-task"],
        "mean_lift": 0.8,
        "scored": 99,
        "unscorable": 0,
        "n": 99,
        "dropped": ["old-task"],
        "best": {"combination": "historical"},
        "measured": 99,
        "unmeasured": 0,
    }
    failed = {**metadata("codex@qwen--dell-failed", fingerprint="f" * 64), **stale}
    measured = metadata("codex@qwen--dell-ok", fingerprint="o" * 64)
    write_combo(root, "failed", failed, artifact="fail")
    write_combo(root, "measured", measured)
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    holdout = patch_current_facts(monkeypatch)
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], holdout, {}))

    def score_one(answers, *_args):
        if "fail" in "\n".join(sum(answers.values(), [])):
            raise A.AgyJudgeError("current Agy failure")
        return [0.8 if "skill answer" in "\n".join(sum(answers.values(), [])) else 0.4]

    monkeypatch.setattr(R, "score", score_one)

    summary = R.rescore("demo", jobs_roots=[root], log=lambda *_args: None)

    failed_row = summary["combinations"][failed["combination"]]
    assert failed_row["error"] == "current Agy failure"
    assert not (set(stale) - {"error"}) & set(failed_row)
    assert summary["scored"] == 1
    assert summary["unscorable"] == 1
    assert summary["mean_lift"] == pytest.approx(0.4)


def test_rescore_rejects_missing_recorded_attempt_before_scoring(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    incomplete = metadata("codex@qwen--dell-incomplete", fingerprint="i" * 64)
    measured = metadata("codex@qwen--dell-ok", fingerprint="o" * 64)
    write_combo(root, "incomplete", incomplete, recorded_attempts=2)
    write_combo(root, "measured", measured)
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    holdout = patch_current_facts(monkeypatch)
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], holdout, {}))
    score_calls = []

    def score_one(answers, *_args):
        score_calls.append(answers)
        return [0.8 if "skill answer" in "\n".join(sum(answers.values(), [])) else 0.4]

    monkeypatch.setattr(R, "score", score_one)

    summary = R.rescore("demo", jobs_roots=[root], log=lambda *_args: None)

    row = summary["combinations"][incomplete["combination"]]
    assert "attempt" in row["error"].lower()
    assert not {"skill_scores", "control_scores", "skill_mean", "control_mean", "lift"} & set(row)
    assert len(score_calls) == 2
    assert summary["scored"] == 1
    assert summary["unscorable"] == 1


def test_distinct_endpoint_fingerprints_remain_separate_measured_rows(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    first = metadata("codex@qwen--dell-a", fingerprint="a" * 64)
    second = metadata("codex@qwen--dell-b", fingerprint="b" * 64)
    write_combo(root, "first", first)
    write_combo(root, "second", second)
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    patch_scoring(monkeypatch)

    summary = R.rescore("demo", jobs_roots=[root], log=lambda *_args: None)

    assert set(summary["combinations"]) == {first["combination"], second["combination"]}
    assert summary["combinations"][first["combination"]]["endpoint_fingerprint"] == "a" * 64
    assert summary["combinations"][second["combination"]]["endpoint_fingerprint"] == "b" * 64
    assert summary["scored"] == 2


def test_rescore_deduplicates_a_combination_when_the_same_root_is_repeated(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    record = metadata("codex@qwen--dell", fingerprint="d" * 64)
    write_combo(root, "one", record)
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    patch_scoring(monkeypatch)

    summary = R.rescore("demo", jobs_roots=[root, root], log=lambda *_args: None)

    assert list(summary["combinations"]) == [record["combination"]]
    assert summary["scored"] == 1


def test_rescore_selects_only_exact_completed_combination_paths(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    complete = metadata("aider@qwen--dell", fingerprint="a" * 64)
    active = metadata("codex@qwen--dell", fingerprint="b" * 64)
    write_combo(root, "complete", complete)
    active_dir = root / "active"
    active_dir.mkdir(parents=True)
    (active_dir / "combo.json").write_text("active evidence must not be read")
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    patch_scoring(monkeypatch)

    summary = R.rescore(
        "demo", jobs_roots=[root], combination_paths=[root / "complete"],
        log=lambda *_args: None)

    assert list(summary["combinations"]) == [complete["combination"]]
    assert summary["scored"] == 1


def test_rescore_rejects_selected_path_outside_jobs_roots_before_preflight(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    write_combo(root, "complete", metadata("aider@qwen--dell", fingerprint="a" * 64))
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    holdout = patch_current_facts(monkeypatch)
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], holdout, {}))
    monkeypatch.setattr(R, "preflight", lambda: pytest.fail("preflight must not start"))

    outside = tmp_path / "outside"
    write_combo(outside, "other", metadata("other@qwen--dell", fingerprint="b" * 64))

    with pytest.raises(ValueError, match="immediate child"):
        R.rescore("demo", jobs_roots=[root], combination_paths=[outside / "other"],
                  log=lambda *_args: None)


def test_explicit_empty_selection_never_discovers_active_siblings(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    active = root / "active"
    active.mkdir(parents=True)
    (active / "combo.json").write_text("must not be read")
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    holdout = patch_current_facts(monkeypatch)
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], holdout, {}))
    monkeypatch.setattr(R, "preflight", lambda: pytest.fail("preflight must not start"))

    with pytest.raises(ValueError, match="no completed combination paths selected"):
        R.rescore("demo", jobs_roots=[root], combination_paths=[], log=lambda *_args: None)


def test_rescore_checkpoints_each_new_lift_to_explicit_output(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    first = metadata("aider@qwen--dell", fingerprint="a" * 64)
    second = metadata("codex@qwen--dell", fingerprint="b" * 64)
    write_combo(root, "first", first)
    write_combo(root, "second", second)
    output = tmp_path / "published" / "build-loop.rescored.json"
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    patch_scoring(monkeypatch)
    writes = []
    real_write = R.atomic_write_json

    def record_write(path, payload):
        writes.append((path, list(payload["combinations"])))
        real_write(path, payload)

    monkeypatch.setattr(R, "atomic_write_json", record_write)

    summary = R.rescore("demo", jobs_roots=[root], output=output, log=lambda *_args: None)

    assert writes == [
        (output, [first["combination"]]),
        (output, [first["combination"], second["combination"]]),
    ]
    assert json.loads(output.read_text()) == summary


def test_rescore_requests_bounded_parallel_agy_grading(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    write_combo(root, "one", metadata("aider@qwen--dell", fingerprint="a" * 64))
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    holdout = patch_current_facts(monkeypatch)
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], holdout, {}))
    concurrencies = []

    def fake_score(_answers, _skill, _holdout, _skipped, concurrency):
        concurrencies.append(concurrency)
        return [0.5]

    monkeypatch.setattr(R, "score", fake_score)

    R.rescore("demo", jobs_roots=[root], log=lambda *_args: None)

    assert concurrencies == [4, 4]


def test_selected_rescore_preserves_prior_compatible_lifts(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    first = metadata("aider@qwen--dell", fingerprint="a" * 64)
    second = metadata("codex@qwen--dell", fingerprint="b" * 64)
    first_path = write_combo(root, "first", first)
    second_path = write_combo(root, "second", second)
    output = tmp_path / "published" / "build-loop.rescored.json"
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    patch_scoring(monkeypatch)
    original_score = R.score
    score_calls = []

    def counted_score(*args, **kwargs):
        score_calls.append(args[0])
        return original_score(*args, **kwargs)

    monkeypatch.setattr(R, "score", counted_score)

    R.rescore("demo", jobs_roots=[root], combination_paths=[first_path], output=output,
              log=lambda *_args: None)
    summary = R.rescore("demo", jobs_roots=[root], combination_paths=[second_path], output=output,
                        log=lambda *_args: None)

    assert list(summary["combinations"]) == [first["combination"], second["combination"]]
    assert summary["scored"] == 2
    assert len(score_calls) == 4  # two arms once per cell; the first cell is not paid twice


def test_current_canary_failure_replaces_its_prior_lift_without_rejudging(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    failed = metadata("aider@qwen--dell", fingerprint="a" * 64)
    unaffected = metadata("codex@qwen--dell", fingerprint="b" * 64)
    failed_path = write_combo(root, "failed", failed)
    unaffected_path = write_combo(root, "unaffected", unaffected)
    output = tmp_path / "published" / "demo.rescored.json"
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    patch_scoring(monkeypatch)

    prior = R.rescore(
        "demo", jobs_roots=[root], combination_paths=[failed_path, unaffected_path],
        output=output, log=lambda *_args: None,
    )
    unaffected_row = prior["combinations"][unaffected["combination"]]
    (failed_path / "combo.json").write_text(json.dumps({
        **failed,
        "canary_error": "current canary failed",
    }))
    monkeypatch.setattr(R, "score", lambda *_args, **_kwargs:
                        pytest.fail("current canary failure invoked the judge"))

    summary = R.rescore(
        "demo", jobs_roots=[root], combination_paths=[failed_path],
        output=output, log=lambda *_args: None,
    )

    failed_row = summary["combinations"][failed["combination"]]
    assert failed_row["error"] == "current canary failed"
    assert not {"lift", "skill_mean", "control_mean", "skill_scores", "control_scores"} & set(
        failed_row)
    assert summary["combinations"][unaffected["combination"]] == unaffected_row
    assert summary["scored"] == 1
    assert summary["mean_lift"] == unaffected_row["lift"]


def test_selected_rescore_preserves_scale_provenance_for_report(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    record = metadata("aider@qwen35-4b--dell", fingerprint="4" * 64)
    record.update({"family": "Qwen3.5", "parameter_billions": 4.0,
                   "quantization": "fp8-load", "tool_parser": "qwen3_coder",
                   "exploratory": True, "rankable": False})
    combo = write_combo(root, "qwen35-4b", record)
    output_root = tmp_path / "published"
    output = output_root / "demo.rescored.json"
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    patch_scoring(monkeypatch)

    R.rescore("demo", jobs_roots=[root], combination_paths=[combo], output=output,
              log=lambda *_args: None)
    matrix = harbor_report.read_matrix("demo", output_root)
    row = matrix["rows"][0]

    assert {key: row[key] for key in (
        "family", "parameter_billions", "quantization", "tool_parser")
    } == {"family": "Qwen3.5", "parameter_billions": 4.0,
          "quantization": "fp8-load", "tool_parser": "qwen3_coder"}
    assert matrix["rankable"] is False
    assert matrix["exploratory"] is True


def test_prior_row_without_treatment_fields_does_not_reuse_lift():
    identity = metadata("aider@qwen--dell", fingerprint="a" * 64)
    identity.update({"family": "Qwen3.5", "parameter_billions": 4.0,
                     "quantization": "fp8-load", "tool_parser": "qwen3_coder"})
    prior = {"prior": {key: value for key, value in identity.items()
                        if key not in {"family", "parameter_billions", "quantization",
                                       "tool_parser"}}}

    assert R._matching_row_key(identity, prior) is None


def test_prior_row_with_stale_gateway_revision_does_not_reuse_lift():
    identity = metadata("codex@qwen--dell", fingerprint="a" * 64)
    identity.update({"gateway_revision": "v8", "gateway_identity": "route-v8"})
    prior = {"prior": {**identity, "gateway_revision": "v7", "lift": 0.5}}

    assert R._matching_row_key(identity, prior) is None


def test_current_runtime_replaces_stale_prior_row_for_same_logical_cell(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    current = metadata("codex@qwen--dell", fingerprint="a" * 64)
    current.update({"gateway_revision": f"route-v8-{R.NATIVE_RUNNER_REVISION}",
                    "gateway_identity": f"route-v8-{R.NATIVE_RUNNER_REVISION}"})
    combo = write_combo(root, "codex-qwen", current)
    output = tmp_path / "demo.rescored.json"
    stale = {**current, "gateway_revision": "route-v8-native-v2",
             "gateway_identity": "route-v8-native-v2", "lift": 0.9,
             "skill_mean": 1.0, "control_mean": 0.1}
    output.write_text(json.dumps({
        "skill": "demo", "task_fingerprint": current["task_fingerprint"],
        "attempts": current["attempts"], "judge": R.AGY_IDENTITY,
        "scoring_revision": "harbor-rubric-v2-agy", "judge_billing_mode": "subscription",
        "judge_runtime": RUNTIME, "combinations": {"stale": stale},
    }))
    patch_scoring(monkeypatch)
    calls = []
    monkeypatch.setattr(R, "score", lambda *_args: calls.append(1) or [0.6])

    result = R.rescore("demo", jobs_roots=[root], combination_paths=[combo], output=output,
                       log=lambda *_args: None)

    assert len(calls) == 2
    assert result["scored"] == 1
    assert len(result["combinations"]) == 1
    [row] = result["combinations"].values()
    assert row["gateway_revision"] == f"route-v8-{R.NATIVE_RUNNER_REVISION}"
    assert row["lift"] == 0.0


def test_current_runtime_wins_over_later_stale_root_independent_of_input_order(tmp_path,
                                                                               monkeypatch):
    current_root, stale_root = tmp_path / "current", tmp_path / "stale"
    base = metadata("codex@qwen--dell", fingerprint="a" * 64)
    current = {**base, "gateway_revision": f"route-v8-{R.NATIVE_RUNNER_REVISION}",
               "gateway_identity": f"route-v8-{R.NATIVE_RUNNER_REVISION}"}
    stale = {**base, "gateway_revision": "route-v8-native-v2",
             "gateway_identity": "route-v8-native-v2"}
    write_combo(current_root, "current", current)
    write_combo(stale_root, "stale", stale)
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path / "published")
    patch_scoring(monkeypatch)

    result = R.rescore("demo", jobs_roots=[current_root, stale_root], log=lambda *_args: None)

    assert result["scored"] == 1
    assert len(result["combinations"]) == 1
    [row] = result["combinations"].values()
    assert row["gateway_revision"] == f"route-v8-{R.NATIVE_RUNNER_REVISION}"


def test_selected_rescore_refuses_incompatible_prior_output(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    combo = write_combo(root, "one", metadata("aider@qwen--dell", fingerprint="a" * 64))
    output = tmp_path / "build-loop.rescored.json"
    output.write_text(json.dumps({
        "skill": "demo", "task_fingerprint": "other", "attempts": 3,
        "judge": R.AGY_IDENTITY, "scoring_revision": "harbor-rubric-v2-agy",
        "judge_billing_mode": "subscription", "judge_runtime": RUNTIME,
        "combinations": {},
    }))
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    patch_scoring(monkeypatch)
    monkeypatch.setattr(R, "score", lambda *_args: pytest.fail("scoring must not start"))

    with pytest.raises(ValueError, match="incompatible existing rescore output"):
        R.rescore("demo", jobs_roots=[root], combination_paths=[combo], output=output,
                  log=lambda *_args: None)


def test_selected_rescore_drops_prior_lifts_from_another_skill_revision(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    first = metadata("aider@qwen--dell", fingerprint="a" * 64)
    second = metadata("codex@qwen--dell", fingerprint="b" * 64)
    second["skill_body"] = "different skill revision\n"
    second["skill_sha256"] = hashlib.sha256(second["skill_body"].encode()).hexdigest()
    first_path = write_combo(root, "first", first)
    second_path = write_combo(root, "second", second)
    output = tmp_path / "build-loop.rescored.json"
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    patch_scoring(monkeypatch)

    R.rescore("demo", jobs_roots=[root], combination_paths=[first_path], output=output,
              log=lambda *_args: None)
    summary = R.rescore("demo", jobs_roots=[root], combination_paths=[second_path], output=output,
                        log=lambda *_args: None)

    assert list(summary["combinations"]) == [second["combination"]]


def test_agy_failure_row_has_current_identity_and_no_measurements(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    failed = metadata("codex@qwen--dell-failed", fingerprint="f" * 64)
    measured = metadata("codex@qwen--dell-ok", fingerprint="o" * 64)
    write_combo(root, "failed", failed, artifact="fail")
    write_combo(root, "measured", measured)
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    holdout = patch_current_facts(monkeypatch)
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], holdout, {}))

    def score_one(answers, *_args):
        if "fail" in "\n".join(sum(answers.values(), [])):
            raise A.AgyJudgeError("judge unavailable")
        return [0.8 if "skill answer" in "\n".join(sum(answers.values(), [])) else 0.4]

    monkeypatch.setattr(R, "score", score_one)
    summary = R.rescore("demo", jobs_roots=[root], log=lambda *_args: None)

    failed_row = summary["combinations"][failed["combination"]]
    assert failed_row["error"] == "judge unavailable"
    assert failed_row["judge"] == R.AGY_IDENTITY
    assert failed_row["scoring_revision"] == "harbor-rubric-v2-agy"
    assert failed_row["judge_runtime"] == RUNTIME
    assert failed_row["judge_billing_mode"] == "subscription"
    assert not {"score", "skill_scores", "control_scores", "skill_mean", "control_mean", "lift"} & set(failed_row)
    row = summary["combinations"][measured["combination"]]
    assert row["attempts"] == 3
    assert row["target_alias"] == "dell"
    assert row["endpoint_fingerprint"] == "o" * 64
    assert row["protocol"] == "openai"


def test_rescore_preserves_failed_canary_as_explicit_unmeasured_row(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    failed = {**metadata("codex@qwen--dell-failed", fingerprint="f" * 64),
              "canary_error": "ApiRateLimitError: local gateway returned 429"}
    measured = metadata("aider@qwen--dell-ok", fingerprint="o" * 64)
    failed_dir = root / "failed"
    failed_dir.mkdir(parents=True)
    (failed_dir / "combo.json").write_text(json.dumps(failed))
    write_combo(root, "measured", measured)
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    patch_scoring(monkeypatch)

    summary = R.rescore("demo", jobs_roots=[root], log=lambda *_args: None)

    row = summary["combinations"][failed["combination"]]
    assert row["error"] == failed["canary_error"]
    assert "canary_error" not in row
    assert not {"skill_mean", "control_mean", "lift", "skill_scores", "control_scores"} & set(row)
    assert summary["scored"] == 1
    assert summary["unscorable"] == 1


def test_rescore_publishes_trailing_failed_canary_in_final_summary(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    measured = metadata("aider@qwen--dell-ok", fingerprint="o" * 64)
    failed = {**metadata("codex@qwen--dell-failed", fingerprint="f" * 64),
              "canary_error": "canary failed"}
    measured_path = write_combo(root, "measured", measured)
    failed_path = root / "failed"
    failed_path.mkdir(parents=True)
    (failed_path / "combo.json").write_text(json.dumps(failed))
    output = tmp_path / "demo.rescored.json"
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    patch_scoring(monkeypatch)

    summary = R.rescore(
        "demo", jobs_roots=[root], combination_paths=[measured_path, failed_path],
        output=output, log=lambda *_args: None)

    assert json.loads(output.read_text()) == summary
    assert summary["combinations"][failed["combination"]]["error"] == "canary failed"


def test_rescore_admits_explicit_partial_arm_measurement_error(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    measured = metadata("aider@qwen--dell-ok", fingerprint="o" * 64)
    partial = {**metadata("codex@qwen--dell-partial", fingerprint="p" * 64),
               "measurement_error": "control arm failed"}
    measured_path = write_combo(root, "measured", measured)
    partial_path = root / "partial"
    partial_path.mkdir(parents=True)
    (partial_path / "combo.json").write_text(json.dumps(partial))
    (partial_path / "skill").mkdir()
    output = tmp_path / "demo.rescored.json"
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    patch_scoring(monkeypatch)

    summary = R.rescore(
        "demo", jobs_roots=[root], combination_paths=[measured_path, partial_path],
        output=output, log=lambda *_args: None)

    row = summary["combinations"][partial["combination"]]
    assert row["error"] == "control arm failed"
    assert "lift" not in row


def test_rescore_refuses_to_publish_when_every_combination_is_unscorable(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    write_combo(root, "failed", metadata("codex@qwen--dell", fingerprint="f" * 64))
    out = tmp_path / "demo.rescored.json"
    out.write_bytes(b"known-good")
    monkeypatch.setattr(R, "HARBOR_DIR", tmp_path)
    holdout = patch_current_facts(monkeypatch)
    monkeypatch.setattr(R, "load_tasks", lambda skill: ([], holdout, {}))
    monkeypatch.setattr(R, "score", lambda *_args: (_ for _ in ()).throw(RuntimeError("judge down")))

    with pytest.raises(SystemExit, match="nothing was measured"):
        R.rescore("demo", jobs_roots=[root], log=lambda *_args: None)

    assert out.read_bytes() == b"known-good"


def test_atomic_write_preserves_existing_bytes_on_serialization_and_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "matrix.json"
    path.write_bytes(b"known-good")

    with pytest.raises(TypeError):
        R.atomic_write_json(path, {"bad": {1, 2}})
    assert path.read_bytes() == b"known-good"
    assert not list(tmp_path.glob(".matrix.json.*.tmp"))

    def broken_replace(source, destination):
        assert json.loads(Path(source).read_text()) == {"next": 1}
        raise OSError("disk failed")

    monkeypatch.setattr(R.os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk failed"):
        R.atomic_write_json(path, {"next": 1})
    assert path.read_bytes() == b"known-good"
    assert not list(tmp_path.glob(".matrix.json.*.tmp"))


def test_atomic_write_replaces_only_a_complete_json_file(tmp_path, monkeypatch):
    path = tmp_path / "matrix.json"
    seen = []
    original_replace = R.os.replace

    def inspect_then_replace(source, destination):
        seen.append(json.loads(Path(source).read_text()))
        original_replace(source, destination)

    monkeypatch.setattr(R.os, "replace", inspect_then_replace)
    R.atomic_write_json(path, {"next": [1, 2]})

    assert seen == [{"next": [1, 2]}]
    assert json.loads(path.read_text()) == {"next": [1, 2]}
    assert not list(tmp_path.glob(".matrix.json.*.tmp"))


def test_native_combo_resolves_exact_sibling_job_and_arm_identity(tmp_path):
    from ingot.optimize.harbor_native import NativeTrialIdentity, identity_env

    root = tmp_path / "jobs"
    combo = root / "aider-cell"
    job = root / "native-full"
    combo.mkdir(parents=True)
    job.mkdir()
    common = dict(combination_id="aider@dot-backbone--dell-qwen-deadbeefcafe",
                  endpoint_fingerprint="deadbeefcafe", harness="aider", protocol="chat",
                  gateway_revision="direct")
    identities = {arm: NativeTrialIdentity(**common, arm=arm)
                  for arm in ("skill", "control")}
    (combo / "combo.json").write_text(json.dumps({
        "combination": common["combination_id"],
        "harness": common["harness"],
        "endpoint_fingerprint": common["endpoint_fingerprint"],
        "protocol": common["protocol"],
        "gateway_revision": common["gateway_revision"],
        "native_job": "native-full",
        "native_identities": {arm: identity_env(identity)
                              for arm, identity in identities.items()},
    }))

    assert R._arm_evidence(combo, "skill") == (job, identities["skill"])
    assert R._arm_evidence(combo, "control") == (job, identities["control"])


def test_native_combo_refuses_lock_identity_mismatched_to_combo_metadata(tmp_path):
    from ingot.optimize.harbor_native import NativeTrialIdentity, identity_env

    combo = tmp_path / "jobs" / "cell"
    combo.mkdir(parents=True)
    common = dict(combination_id="aider@dot-backbone--dell-qwen-deadbeefcafe",
                  endpoint_fingerprint="deadbeefcafe", harness="aider", protocol="chat",
                  gateway_revision="direct")
    (combo / "combo.json").write_text(json.dumps({
        "combination": "aider@other--dell-qwen-deadbeefcafe",
        "harness": "aider", "endpoint_fingerprint": "deadbeefcafe",
        "protocol": "chat", "gateway_revision": "direct", "native_job": "native-full",
        "native_identities": {
            arm: identity_env(NativeTrialIdentity(**common, arm=arm))
            for arm in ("skill", "control")},
    }))

    with pytest.raises(ValueError, match="does not match combo metadata"):
        R._arm_evidence(combo, "skill")


@pytest.mark.parametrize("native_job", ["../other", "/tmp/other", "http:job"])
def test_native_combo_refuses_non_sibling_job_reference(tmp_path, native_job):
    combo = tmp_path / "jobs" / "cell"
    combo.mkdir(parents=True)
    (combo / "combo.json").write_text(json.dumps({
        "native_job": native_job,
        "native_identities": {},
    }))

    with pytest.raises(ValueError, match="native job"):
        R._arm_evidence(combo, "skill")


def test_discovery_skips_only_native_job_declared_by_a_combo(tmp_path):
    root = tmp_path / "jobs"
    combo = root / "cell"
    native = root / "native-full"
    combo.mkdir(parents=True)
    native.mkdir()
    (combo / "combo.json").write_text(json.dumps({
        **metadata("aider@dot-backbone--dell-qwen-deadbeefcafe",
                   fingerprint="deadbeefcafe"),
        "native_job": "native-full",
    }))

    assert R.discover_combinations([root]) == [combo]

    (root / "native-full--aider--other").mkdir()
    assert R.discover_combinations([root]) == [combo]


def test_discovery_rejects_a_claimed_combo_with_malformed_identity(tmp_path):
    root = tmp_path / "jobs"
    malformed = root / "malformed"
    malformed.mkdir(parents=True)
    (malformed / "combo.json").write_text("[]")

    with pytest.raises(ValueError, match="no combo identity"):
        R.discover_combinations([root])
