"""Harbor attempt export and receipt validation without live telemetry calls."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from langfuse import Langfuse

from ingot.optimize import harbor_langfuse as L


FIXTURE = Path(__file__).parent / "fixtures" / "harbor" / "langfuse-trial"
METADATA = {
    "combination": "codex@fixture-model--dell-fixture",
    "harness": "codex",
    "model": "fixture-model",
    "target_alias": "dell-fixture",
    "endpoint_fingerprint": "f" * 64,
    "protocol": "openai",
    "task_fingerprint": "t" * 64,
    "attempts": 1,
    "arm": "skill",
}
SKILL_BODY = "fixture skill body\n"
PROVENANCE_METADATA = {
    **METADATA,
    "skill": "demo",
    "skill_body": SKILL_BODY,
    "skill_sha256": hashlib.sha256(SKILL_BODY.encode()).hexdigest(),
    "task_texts": {"fixture-h0": "Use only deterministic fixture input."},
}


def copy_trial(tmp_path: Path) -> Path:
    trial = tmp_path / "job" / "fixture-h0__attempt-1"
    shutil.copytree(FIXTURE, trial)
    return trial


def payload_sha256(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class FakeObservation:
    def __init__(self) -> None:
        self.ended = 0
        self.id = ""

    def end(self) -> None:
        self.ended += 1


class FakeLangfuse:
    def __init__(self, *, readback: str = "matching") -> None:
        self.readback = readback
        self.seeds: list[str] = []
        self.observations: list[dict] = []
        self.observation = FakeObservation()
        self.flushes = 0
        self.reads: list[str] = []

    def create_trace_id(self, *, seed: str) -> str:
        self.seeds.append(seed)
        return Langfuse.create_trace_id(seed=seed)

    def start_observation(self, **kwargs):
        self.observations.append(kwargs)
        self.observation.id = f"{len(self.observations):016x}"
        return self.observation

    def flush(self) -> None:
        self.flushes += 1

    def read_trace(self, trace_id: str):
        self.reads.append(trace_id)
        if self.readback == "absent" or not self.observations:
            return None
        metadata = dict(self.observations[-1]["metadata"])
        if self.readback == "wrong-hash":
            metadata["payload_sha256"] = "0" * 64
        if self.readback == "wrong-evidence":
            metadata["telemetry_evidence"] = {
                **metadata.get("telemetry_evidence", {}), "model": "wrong-model",
            }
        if self.readback == "wrong-id":
            trace_id = "0" * 32
        return {"id": trace_id, "observations": [
            {"id": self.observation.id, "name": "harbor-attempt",
             "type": "AGENT", "metadata": metadata},
        ]}


class FakeSdkOnly:
    """Langfuse SDK surface without the test-only read_trace seam."""

    def __init__(self) -> None:
        self.delegate = FakeLangfuse()

    @property
    def observations(self):
        return self.delegate.observations

    def create_trace_id(self, *, seed: str) -> str:
        return self.delegate.create_trace_id(seed=seed)

    def start_observation(self, **kwargs):
        return self.delegate.start_observation(**kwargs)

    def flush(self) -> None:
        self.delegate.flush()


def test_payload_captures_attempt_evidence_and_structurally_excludes_sensitive_fields(tmp_path):
    """Removing a retained evidence field or copying Harbor config/env data must fail this test."""
    trial = copy_trial(tmp_path)

    payload = L.build_attempt_payload(trial, {
        **METADATA,
        "base_url": "https://metadata-endpoint.invalid/v1",
        "skill_body": "PRIVATE SKILL BODY",
        "skill_sha256": hashlib.sha256(b"PRIVATE SKILL BODY").hexdigest(),
        "environment": {"TOKEN": "metadata-secret"},
    })

    assert payload == {
        "exporter_revision": "harbor-langfuse-v3",
        "attempt": {
            "id": "00000000-0000-0000-0000-000000000001",
            "trial_name": "fixture-h0__attempt-1",
        },
        "task": {
            "name": "ingot/fixture-h0",
            "checksum": "fixture-task-checksum",
            "source": "fixture",
            "text": "",
        },
        "skill": "",
        "trajectory": {
            "steps": [
                {"source": "user", "timestamp": "2000-01-01T00:00:00Z"},
                {"source": "agent", "timestamp": "2000-01-01T00:00:00Z"},
                {"source": "agent", "timestamp": "2000-01-01T00:00:01Z"},
            ],
        },
        "verifier_output": {"test-stdout.txt": ""},
        "solution_artifacts": {
            "_objective_check.txt": (
                "Fixture objective check\n\n"
                "- deterministic output exists\n"
                "- no external endpoint was contacted\n"
                "- no credential was used\n\nPASS\n"
            )
        },
        "status": "failed",
        "exception_category": "AgentSetupTimeoutError",
        "error_detail": "sanitized fixture failure",
        "timestamps": {
            "started_at": "2000-01-01T00:00:00Z",
            "finished_at": "2000-01-01T00:00:01Z",
            "error_at": "2000-01-01T00:00:00Z",
        },
        "usage": {
            "input_tokens": 120,
            "cache_tokens": 40,
            "output_tokens": 30,
            "cost_usd": 0.01,
        },
        "model": "fixture-model",
        "metadata": {
            **METADATA,
            "skill_sha256": hashlib.sha256(b"PRIVATE SKILL BODY").hexdigest(),
        },
    }
    rendered = json.dumps(payload, sort_keys=True)
    for forbidden in (
        "fixture-secret-must-not-export",
        "fixture-endpoint.invalid",
        "metadata-endpoint.invalid",
        "metadata-secret",
        "PRIVATE SKILL BODY",
        "/fixtures/skills/private-skill/SKILL.md",
        '"environment"',
    ):
        assert forbidden not in rendered


def test_payload_bounds_text_and_omits_binary_solution_artifacts(tmp_path):
    """An oversized or binary solution must never cross the telemetry boundary."""
    trial = copy_trial(tmp_path)
    solution = trial / "verifier" / "solution"
    (solution / "long.txt").write_text("prefix-" + "x" * 3000 + "-tail")
    (solution / "output.bin").write_bytes(b"\x00\xff\x00private")

    payload = L.build_attempt_payload(trial, METADATA)

    assert "output.bin" not in payload["solution_artifacts"]
    assert len(payload["solution_artifacts"]["long.txt"]) <= 2000
    assert payload["solution_artifacts"]["long.txt"].endswith("-tail")


def test_payload_accepts_a_bounded_large_trajectory_and_compacts_it(tmp_path):
    """Harbor trajectories exceed artifact limits but export only a compact safe projection."""
    trial = copy_trial(tmp_path)
    trajectory_path = trial / "agent" / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    trajectory["steps"][0]["message"] = "x" * L._MAX_TEXT_BYTES
    trajectory_path.write_text(json.dumps(trajectory))
    assert trajectory_path.stat().st_size > L._MAX_TEXT_BYTES

    payload = L.build_attempt_payload(trial, METADATA)

    assert len(payload["trajectory"]["steps"]) == 3
    assert "message" not in json.dumps(payload["trajectory"])


def test_payload_accepts_observed_full_arm_goose_trajectory_and_compacts_it(tmp_path):
    """Goose emitted a 3,368,055-byte trajectory whose freeform fields must be discarded."""
    trial = copy_trial(tmp_path)
    trajectory_path = trial / "agent" / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    trajectory["steps"][0]["message"] = "PRIVATE-GOOSE-MARKER" + "x" * (13 * 256 * 1024)
    trajectory_path.write_text(json.dumps(trajectory))
    assert trajectory_path.stat().st_size > 3 * 1024 * 1024

    payload = L.build_attempt_payload(trial, METADATA)

    assert len(payload["trajectory"]["steps"]) == 3
    assert "PRIVATE-GOOSE-MARKER" not in json.dumps(payload["trajectory"])


def test_export_discovers_a_valid_large_trajectory_and_compacts_it(tmp_path):
    trial = copy_trial(tmp_path)
    trajectory_path = trial / "agent" / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    trajectory["steps"][0]["message"] = "PRIVATE-LARGE-MARKER" + "x" * L._MAX_TEXT_BYTES
    trajectory_path.write_text(json.dumps(trajectory))
    assert trajectory_path.stat().st_size > L._MAX_TEXT_BYTES
    client = FakeLangfuse()

    receipts = L.export_job_attempts(trial.parent, METADATA, client=client)

    assert len(receipts) == 1
    assert len(client.observations) == 1
    assert "PRIVATE-LARGE-MARKER" not in json.dumps(client.observations)


def test_payload_rejects_a_trajectory_above_its_dedicated_bound(tmp_path):
    """The larger fixed-file allowance must remain bounded against agent-controlled input."""
    trial = copy_trial(tmp_path)
    trajectory_path = trial / "agent" / "trajectory.json"
    with trajectory_path.open("wb") as handle:
        handle.seek(L._MAX_TRAJECTORY_BYTES)
        handle.write(b"tail")

    with pytest.raises(L.TelemetryReceiptError, match="byte budget"):
        L.build_attempt_payload(trial, METADATA)


def test_payload_projects_only_allowlisted_trajectory_fields(tmp_path):
    """Agent step IDs and arbitrary numeric metrics cannot cross the telemetry boundary."""
    trial = copy_trial(tmp_path)
    trajectory_path = trial / "agent" / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    trajectory["final_metrics"] = {
        "total_prompt_tokens": 80,
        "total_cached_tokens": 25,
        "total_completion_tokens": 20,
        "total_cost_usd": 0.005,
        "untrusted_numeric_field": 123,
    }
    trajectory_path.write_text(json.dumps(trajectory))

    projected = L.build_attempt_payload(trial, METADATA)["trajectory"]

    assert all("step_id" not in step for step in projected["steps"])
    assert projected["final_metrics"] == {
        "total_prompt_tokens": 80,
        "total_cached_tokens": 25,
        "total_completion_tokens": 20,
        "total_cost_usd": 0.005,
    }


def test_payload_treats_missing_trajectory_steps_as_empty(tmp_path):
    trial = copy_trial(tmp_path)
    trajectory_path = trial / "agent" / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    del trajectory["steps"]
    trajectory_path.write_text(json.dumps(trajectory))

    assert L.build_attempt_payload(trial, METADATA)["trajectory"]["steps"] == []


def test_payload_rejects_non_list_trajectory_steps(tmp_path):
    trial = copy_trial(tmp_path)
    trajectory_path = trial / "agent" / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    trajectory["steps"] = {"source": "agent"}
    trajectory_path.write_text(json.dumps(trajectory))

    with pytest.raises(L.TelemetryReceiptError, match="trajectory steps"):
        L.build_attempt_payload(trial, METADATA)


def test_payload_trajectory_projection_drops_freeform_strings(tmp_path):
    trial = copy_trial(tmp_path)
    trajectory_path = trial / "agent" / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    trajectory["schema_version"] = "PRIVATE-SCHEMA-MARKER"
    trajectory["session_id"] = "PRIVATE-SESSION-MARKER"
    trajectory["agent"] = {
        "name": "PRIVATE-AGENT-MARKER",
        "version": "PRIVATE-VERSION-MARKER",
        "model_name": "PRIVATE-MODEL-MARKER",
    }
    trajectory["steps"] = [
        {"source": "user", "timestamp": "2000-01-01T00:00:00Z", "message": "PRIVATE"},
        {"source": "agent", "timestamp": "2000-01-01T00:00:01Z"},
        {"source": "tool", "timestamp": "2000-01-01T00:00:02Z"},
        {"source": "PRIVATE-SOURCE-MARKER", "timestamp": "not-a-timestamp"},
    ]
    trajectory_path.write_text(json.dumps(trajectory))

    projected = L.build_attempt_payload(trial, METADATA)["trajectory"]

    assert projected == {"steps": [
        {"source": "user", "timestamp": "2000-01-01T00:00:00Z"},
        {"source": "agent", "timestamp": "2000-01-01T00:00:01Z"},
        {"timestamp": "2000-01-01T00:00:02Z"},
        {},
    ]}


def test_payload_model_does_not_fall_back_to_freeform_trajectory_agent(tmp_path):
    trial = copy_trial(tmp_path)
    result_path = trial / "result.json"
    result = json.loads(result_path.read_text())
    result["agent_info"] = {}
    result_path.write_text(json.dumps(result))
    trajectory_path = trial / "agent" / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    trajectory["agent"]["model_name"] = "PRIVATE-MODEL-MARKER"
    trajectory_path.write_text(json.dumps(trajectory))
    metadata = {key: value for key, value in METADATA.items() if key != "model"}

    assert L.build_attempt_payload(trial, metadata)["model"] == ""


def test_payload_rejects_solution_symlink_outside_the_verifier_root(tmp_path):
    """Following an agent-controlled artifact symlink would export a parent-process host file."""
    trial = copy_trial(tmp_path)
    outside = tmp_path / "parent-secret.txt"
    outside.write_text("outside-parent-secret")
    artifact = trial / "verifier" / "solution" / "outside.txt"
    artifact.symlink_to(outside)

    with pytest.raises(L.TelemetryReceiptError, match="admission"):
        L.build_attempt_payload(trial, METADATA)


def test_payload_omits_large_sparse_artifact_before_decoding(tmp_path, monkeypatch):
    """A sparse optional artifact is omitted by size before its contents are allocated."""
    trial = copy_trial(tmp_path)
    artifact = trial / "verifier" / "solution" / "large.txt"
    with artifact.open("wb") as handle:
        handle.seek(8 * 1024 * 1024)
        handle.write(b"tail")
    original_read_bytes = Path.read_bytes

    def reject_full_read(path):
        if path == artifact:
            pytest.fail("oversized artifact was read before its byte cap was checked")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_full_read)

    payload = L.build_attempt_payload(trial, METADATA)

    assert "large.txt" not in payload["solution_artifacts"]
    assert payload["artifact_projection"]["omitted_files"] == 1


def test_payload_rejects_nul_free_control_byte_binary(tmp_path):
    """Valid UTF-8 with binary control bytes is not a text artifact."""
    trial = copy_trial(tmp_path)
    artifact = trial / "verifier" / "solution" / "control.txt"
    artifact.write_bytes(b"visible\x01binary\x02content")

    payload = L.build_attempt_payload(trial, METADATA)

    assert "control.txt" not in payload["solution_artifacts"]


def test_export_rejects_a_symlinked_trial_before_reading_outside_the_job(tmp_path):
    """A job entry cannot redefine an outside directory as a persisted attempt."""
    outside_trial = copy_trial(tmp_path / "outside")
    job = tmp_path / "job"
    job.mkdir()
    linked_trial = job / "fixture-h0__attempt-1"
    linked_trial.symlink_to(outside_trial, target_is_directory=True)
    client = FakeLangfuse()

    with pytest.raises(L.TelemetryReceiptError, match="symlink|directory|admission"):
        L.export_job_attempts(job, METADATA, client=client)

    assert client.observations == []
    assert not (outside_trial / L.RECEIPT_NAME).exists()


def test_export_rejects_a_nonregular_verifier_entry(tmp_path):
    """Silently skipping an agent-controlled pipe would make exported evidence partial."""
    trial = copy_trial(tmp_path)
    os.mkfifo(trial / "verifier" / "solution" / "stream.txt")
    client = FakeLangfuse()

    with pytest.raises(L.TelemetryReceiptError, match="non-regular"):
        L.export_job_attempts(trial.parent, METADATA, client=client)

    assert client.observations == []
    assert not (trial / L.RECEIPT_NAME).exists()


def test_export_rejects_an_intermediate_directory_swap(tmp_path, monkeypatch):
    """A renamed solution directory cannot redirect a later open outside the attempt."""
    trial = copy_trial(tmp_path)
    solution = trial / "verifier" / "solution"
    artifact = solution / "answer.txt"
    artifact.write_text("inside fixture text")
    outside = tmp_path / "outside-solution"
    outside.mkdir()
    (outside / "answer.txt").write_text("PARENT-ONLY-SECRET")
    original_open = os.open
    swapped = False

    def swap_before_artifact_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and Path(path).name == "answer.txt":
            swapped = True
            held = solution.with_name("held-solution")
            solution.rename(held)
            solution.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_artifact_open)
    client = FakeLangfuse()

    with pytest.raises(L.TelemetryReceiptError, match="changed|admission"):
        L.export_job_attempts(trial.parent, METADATA, client=client)

    assert client.observations == []
    assert not (trial / L.RECEIPT_NAME).exists()


def test_export_projects_many_small_artifacts_and_hidden_housekeeping_deterministically(tmp_path):
    """Optional telemetry stays bounded without rejecting authoritative Harbor evidence."""
    trial = copy_trial(tmp_path)
    solution = trial / "verifier" / "solution"
    hidden_target = tmp_path / "hidden-backup"
    hidden_target.mkdir()
    (hidden_target / "secret.txt").write_text("must-not-export")
    (solution / ".backups").symlink_to(hidden_target, target_is_directory=True)
    for index in range(129):
        (solution / f"part-{index:03}.txt").write_text("x")
    client = FakeLangfuse()

    receipts = L.export_job_attempts(trial.parent, METADATA, client=client)

    assert len(receipts) == len(client.observations) == 1
    payload = client.observations[0]["output"]
    exported = payload["solution_artifacts"]
    assert "part-000.txt" in exported
    assert "part-128.txt" not in exported
    assert ".backups" not in json.dumps(payload)
    assert "must-not-export" not in json.dumps(payload)
    assert payload["artifact_projection"]["omitted_hidden_paths"] == 1
    assert payload["artifact_projection"]["omitted_files"] >= 1
    assert payload["artifact_projection"]["exported_files"] <= L._MAX_ARTIFACT_PATHS
    assert (trial / L.RECEIPT_NAME).is_file()


def test_export_projects_aggregate_artifact_bytes_above_the_attempt_budget(tmp_path):
    """Optional files beyond the byte ceiling are omitted without blocking evidence."""
    trial = copy_trial(tmp_path)
    solution = trial / "verifier" / "solution"
    for index in range(80):
        (solution / f"chunk-{index:03}.txt").write_text("x" * 2000)
    client = FakeLangfuse()

    receipts = L.export_job_attempts(trial.parent, METADATA, client=client)

    assert len(receipts) == len(client.observations) == 1
    payload = client.observations[0]["output"]
    assert payload["artifact_projection"]["exported_bytes"] <= L._MAX_ARTIFACT_BYTES
    assert payload["artifact_projection"]["omitted_files"] > 0
    assert (trial / L.RECEIPT_NAME).is_file()


def test_export_prioritizes_solution_answer_over_verifier_noise(tmp_path):
    trial = copy_trial(tmp_path)
    verifier = trial / "verifier"
    solution = verifier / "solution"
    (solution / "answer.md").write_text("graded deliverable")
    for index in range(130):
        (verifier / f"noise-{index:03}.txt").write_text("noise")
    client = FakeLangfuse()

    L.export_job_attempts(trial.parent, METADATA, client=client)

    payload = client.observations[0]["output"]
    assert payload["solution_artifacts"]["answer.md"] == "graded deliverable"
    assert client.observations[0]["metadata"]["exporter_revision"] == "harbor-langfuse-v3"


def test_export_migrates_verified_v2_receipt_without_rerunning_attempt(tmp_path):
    trial = copy_trial(tmp_path)
    client = FakeLangfuse()
    old_digest = "a" * 64
    old_trace = Langfuse.create_trace_id(seed=f"harbor-langfuse-v2:{old_digest}")
    evidence = L._telemetry_evidence(L.build_attempt_payload(trial, METADATA))
    old_receipt = {
        "status": "verified", "trace_id": old_trace, "payload_sha256": old_digest,
        "exporter_revision": "harbor-langfuse-v2",
    }
    (trial / L.RECEIPT_NAME).write_text(json.dumps(old_receipt))
    client.observation.id = "old-observation"
    client.observations.append({"metadata": {
        "payload_sha256": old_digest, "exporter_revision": "harbor-langfuse-v2",
        "telemetry_evidence": evidence,
    }})
    default_read = client.read_trace
    new_reads = 0

    def versioned_read(trace_id):
        nonlocal new_reads
        if trace_id == old_trace:
            return {"id": old_trace, "observations": [{
                "id": "old-observation", "name": "harbor-attempt", "type": "AGENT",
                "metadata": client.observations[0]["metadata"],
            }]}
        if len(client.observations) == 1:
            new_reads += 1
            if new_reads == 1:
                raise TimeoutError("transient v3 read failure")
            return None
        return default_read(trace_id)

    client.read_trace = versioned_read

    with pytest.raises(TimeoutError, match="transient v3 read failure"):
        L.export_job_attempts(trial.parent, METADATA, client=client)

    assert json.loads((trial / L.RECEIPT_NAME).read_text()) == old_receipt
    assert not (trial / L.LEGACY_V2_RECEIPT_NAME).exists()

    [receipt] = L.export_job_attempts(trial.parent, METADATA, client=client)

    assert json.loads((trial / L.LEGACY_V2_RECEIPT_NAME).read_text()) == old_receipt
    assert receipt["status"] == "verified"
    assert receipt["exporter_revision"] == "harbor-langfuse-v3"
    assert json.loads((trial / L.RECEIPT_NAME).read_text()) == receipt
    assert len(client.observations) == 2


def test_export_projects_scan_and_depth_exhaustion(tmp_path, monkeypatch):
    trial = copy_trial(tmp_path)
    solution = trial / "verifier" / "solution"
    nested = solution
    for index in range(4):
        nested = nested / f"level-{index}"
        nested.mkdir()
    (nested / "deep.txt").write_text("deep")
    (solution / "first.txt").write_text("first")
    (solution / "second.txt").write_text("second")
    monkeypatch.setattr(L, "_MAX_ARTIFACT_DEPTH", 2)
    monkeypatch.setattr(L, "_MAX_ARTIFACT_SCAN_PATHS", 6)
    client = FakeLangfuse()

    L.export_job_attempts(trial.parent, METADATA, client=client)

    projection = client.observations[0]["output"]["artifact_projection"]
    assert projection["omitted_files"] > 0
    assert len(client.observations) == 1


def test_payload_uses_trajectory_metrics_when_result_usage_is_unavailable(tmp_path):
    """Dropping Harbor's alternate usage shape would erase usage from successful adapters."""
    trial = copy_trial(tmp_path)
    result_path = trial / "result.json"
    result = json.loads(result_path.read_text())
    result["agent_result"] = {
        "n_input_tokens": None, "n_cache_tokens": None,
        "n_output_tokens": None, "cost_usd": None,
    }
    result_path.write_text(json.dumps(result))
    trajectory_path = trial / "agent" / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    trajectory["final_metrics"] = {
        "total_prompt_tokens": 80,
        "total_cached_tokens": 25,
        "total_completion_tokens": 20,
        "total_cost_usd": 0.005,
    }
    trajectory_path.write_text(json.dumps(trajectory))

    assert L.build_attempt_payload(trial, METADATA)["usage"] == {
        "input_tokens": 80,
        "cache_tokens": 25,
        "output_tokens": 20,
        "cost_usd": 0.005,
    }


def test_canonical_payload_and_hash_do_not_depend_on_parent_secret_environment(tmp_path, monkeypatch):
    """A credential rotation must not change the identity of unchanged persisted evidence."""
    trial = copy_trial(tmp_path)
    trajectory_path = trial / "agent" / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    trajectory["steps"][0]["message"] = "rotated-value"
    trajectory_path.write_text(json.dumps(trajectory))
    (trial / "verifier" / "solution" / "answer.txt").write_text("rotated-value")
    monkeypatch.delenv("CUSTOM_API_KEY", raising=False)
    first = L.build_attempt_payload(trial, PROVENANCE_METADATA)
    first_hash = payload_sha256(first)
    monkeypatch.setenv("CUSTOM_API_KEY", "rotated-value")

    second = L.build_attempt_payload(trial, PROVENANCE_METADATA)

    assert second == first
    assert payload_sha256(second) == first_hash


def test_known_skill_body_is_omitted_from_trajectory_and_solution_artifacts(tmp_path):
    """Known instruction content must not be exported as attempt evidence."""
    trial = copy_trial(tmp_path)
    skill_body = "PRIVATE SKILL BODY\n"
    metadata = {
        **PROVENANCE_METADATA,
        "skill_body": skill_body,
        "skill_sha256": hashlib.sha256(skill_body.encode()).hexdigest(),
    }
    trajectory_path = trial / "agent" / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    trajectory["steps"][0]["message"] = skill_body
    trajectory_path.write_text(json.dumps(trajectory))
    (trial / "verifier" / "solution" / "skill.md").write_text(skill_body)

    payload = L.build_attempt_payload(trial, metadata)

    assert "PRIVATE SKILL BODY" not in json.dumps(payload)
    assert "message" not in json.dumps(payload["trajectory"])
    assert "skill.md" not in payload["solution_artifacts"]


def test_named_sensitive_artifact_classes_are_structurally_excluded(tmp_path):
    """Known config, environment, credential, secret, and endpoint dumps never export."""
    trial = copy_trial(tmp_path)
    solution = trial / "verifier" / "solution"
    for name in (
        "config.json", "configuration.txt", "environment.json", "env.txt",
        "credential.txt", "credentials.log", "secret.txt", "secrets.yaml",
        "endpoint.json", "endpoints.txt",
    ):
        (solution / name).write_text(f"private marker from {name}")

    payload = L.build_attempt_payload(trial, METADATA)

    rendered = json.dumps(payload["solution_artifacts"])
    assert "private marker" not in rendered


def test_artifact_wrapping_the_verified_known_skill_body_is_excluded(tmp_path):
    """Adding a heading cannot disguise the exact producer-supplied skill body."""
    trial = copy_trial(tmp_path)
    skill_body = "PRIVATE SKILL BODY\n"
    metadata = {
        **PROVENANCE_METADATA,
        "skill_body": skill_body,
        "skill_sha256": hashlib.sha256(skill_body.encode()).hexdigest(),
    }
    wrapped = trial / "verifier" / "solution" / "wrapped.md"
    wrapped.write_text(f"# Copied instructions\n\n{skill_body}\nFixture answer")

    payload = L.build_attempt_payload(trial, metadata)

    assert "wrapped.md" not in payload["solution_artifacts"]
    assert "PRIVATE SKILL BODY" not in json.dumps(payload)


def _shape_trial(tmp_path: Path, shape: str) -> Path:
    trial = tmp_path / shape / "fixture-h0__attempt-1"
    trial.mkdir(parents=True)
    if shape in {"success", "failure"}:
        result = {
            "id": f"fixture-{shape}",
            "task_name": "ingot/fixture-h0",
            "trial_name": "fixture-h0__attempt-1",
            "task_checksum": "fixture-checksum",
            "source": "fixture",
            "started_at": "2000-01-01T00:00:00Z",
            "finished_at": "2000-01-01T00:00:01Z",
            "exception_info": None,
        }
        if shape == "failure":
            result["exception_info"] = {
                "exception_type": "RuntimeError",
                "exception_message": "bounded fixture failure",
                "exception_traceback": "private traceback must not export",
                "occurred_at": "2000-01-01T00:00:00.500Z",
            }
        (trial / "result.json").write_text(json.dumps(result))
    elif shape == "trajectory-only":
        trajectory = trial / "agent" / "trajectory.json"
        trajectory.parent.mkdir()
        trajectory.write_text(json.dumps({
            "schema_version": "ATIF-v1.1",
            "session_id": "fixture-session",
            "agent": {"name": "codex", "version": "v1", "model_name": "fixture-model"},
            "steps": [
                {"step_id": 1, "timestamp": "2000-01-01T00:00:00Z",
                 "source": "agent", "message": "private free text"},
                {"step_id": 2, "timestamp": "2000-01-01T00:00:01Z",
                 "source": "agent", "message": "private free text"},
            ],
        }))
    elif shape == "verifier-only":
        verifier = trial / "verifier" / "test-stdout.txt"
        verifier.parent.mkdir()
        verifier.write_text("fixture verifier output")
    elif shape == "solution-only":
        solution = trial / "verifier" / "solution" / "answer.md"
        solution.parent.mkdir(parents=True)
        solution.write_text("fixture solution output")
    return trial


@pytest.mark.parametrize("shape, expected", [
    ("success", {
        "status": "succeeded", "error_detail": None,
        "timestamps": {"started_at": "2000-01-01T00:00:00Z",
                       "finished_at": "2000-01-01T00:00:01Z"},
    }),
    ("failure", {
        "status": "failed", "error_detail": "bounded fixture failure",
        "timestamps": {"started_at": "2000-01-01T00:00:00Z",
                       "finished_at": "2000-01-01T00:00:01Z",
                       "error_at": "2000-01-01T00:00:00.500Z"},
    }),
    ("trajectory-only", {
        "status": "incomplete", "error_detail": None,
        "timestamps": {"started_at": "2000-01-01T00:00:00Z",
                       "finished_at": "2000-01-01T00:00:01Z"},
    }),
    ("verifier-only", {"status": "incomplete", "error_detail": None, "timestamps": {}}),
    ("solution-only", {"status": "incomplete", "error_detail": None, "timestamps": {}}),
])
def test_attempt_shapes_have_exact_terminal_status_and_provenance(tmp_path, shape, expected):
    """Missing terminal result evidence must never be promoted to a successful status."""
    payload = L.build_attempt_payload(_shape_trial(tmp_path, shape), PROVENANCE_METADATA)

    assert {key: payload[key] for key in ("status", "error_detail", "timestamps")} == expected
    assert payload["skill"] == "demo"
    assert payload["task"] == {
        "name": "ingot/fixture-h0" if shape in {"success", "failure"} else "fixture-h0",
        "checksum": "fixture-checksum" if shape in {"success", "failure"} else "",
        "source": "fixture" if shape in {"success", "failure"} else "",
        "text": "Use only deterministic fixture input.",
    }


def test_export_writes_receipt_only_after_deterministic_trace_readback(tmp_path):
    """A wrong trace seed, observation contract, or unverified receipt must fail this test."""
    trial = copy_trial(tmp_path)
    client = FakeLangfuse()

    receipts = L.export_job_attempts(trial.parent, METADATA, client=client)

    payload = L.build_attempt_payload(trial, METADATA)
    digest = payload_sha256(payload)
    trace_id = Langfuse.create_trace_id(seed=f"harbor-langfuse-v3:{digest}")
    expected = {
        "status": "verified",
        "trace_id": trace_id,
        "payload_sha256": digest,
        "exporter_revision": "harbor-langfuse-v3",
    }
    assert receipts == [expected]
    assert json.loads((trial / "langfuse-receipt.json").read_text()) == expected
    assert client.seeds == [f"harbor-langfuse-v3:{digest}"]
    assert client.flushes == 1 and client.reads == [expected["trace_id"], expected["trace_id"]]
    assert client.observation.ended == 1
    observation = client.observations[0]
    assert observation["trace_context"] == {"trace_id": expected["trace_id"]}
    assert observation["name"] == "harbor-attempt" and observation["as_type"] == "agent"
    assert observation["input"] == payload["task"]
    assert observation["output"]["status"] == "failed"
    assert observation["metadata"]["payload_sha256"] == digest
    assert observation["metadata"]["telemetry_evidence"] == {
        "revision": "harbor-agent-evidence-v1",
        "model": "fixture-model",
        "status": "failed",
        "tokens": {"input_tokens": 120, "cache_tokens": 40, "output_tokens": 30},
    }
    assert "model" not in observation
    assert "usage_details" not in observation
    assert "cost_details" not in observation
    assert observation["level"] == "ERROR" and observation["status_message"] == "failed"


def test_real_langfuse_agent_sink_uses_sdk_ids_for_two_payloads_with_existing_provider(tmp_path):
    """Langfuse 4.14 owns span IDs while bounded agent metadata survives its real agent path."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    first = copy_trial(tmp_path)
    second = first.parent / "fixture-h1__attempt-2"
    shutil.copytree(FIXTURE, second)
    result_path = second / "result.json"
    result = json.loads(result_path.read_text())
    result["id"] = "00000000-0000-0000-0000-000000000002"
    result["task_name"] = "ingot/fixture-h1"
    result["trial_name"] = second.name
    result_path.write_text(json.dumps(result))
    preexisting_sink = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(preexisting_sink))
    sink = InMemorySpanExporter()
    sdk = Langfuse(
        public_key="pk-task4-real-sink-two-payloads",
        secret_key="sk-offline-fixture",
        tracer_provider=provider,
        span_exporter=sink,
    )

    class SinkReadback:
        def create_trace_id(self, **kwargs):
            return sdk.create_trace_id(**kwargs)

        def start_observation(self, **kwargs):
            return sdk.start_observation(**kwargs)

        def flush(self):
            sdk.flush()

        def read_trace(self, trace_id):
            observations = []
            for span in sink.get_finished_spans():
                if f"{span.context.trace_id:032x}" != trace_id:
                    continue
                metadata = {}
                prefix = "langfuse.observation.metadata."
                for key, value in span.attributes.items():
                    if key.startswith(prefix):
                        try:
                            metadata[key.removeprefix(prefix)] = json.loads(value)
                        except (TypeError, ValueError):
                            metadata[key.removeprefix(prefix)] = value
                observations.append({
                    "id": f"{span.context.span_id:016x}",
                    "name": span.name,
                    "type": span.attributes["langfuse.observation.type"],
                    "metadata": metadata,
                })
            return {"id": trace_id, "observations": observations} if observations else None

    receipts = L.export_job_attempts(first.parent, METADATA, client=SinkReadback())

    spans = sink.get_finished_spans()
    assert len(receipts) == 2 and all(item["status"] == "verified" for item in receipts)
    assert len(spans) == 2
    assert len(preexisting_sink.get_finished_spans()) == 2
    assert len({span.context.span_id for span in spans}) == 2
    for span in spans:
        assert span.attributes["langfuse.observation.type"] == "agent"
        evidence = json.loads(span.attributes["langfuse.observation.metadata.telemetry_evidence"])
        assert evidence == {
            "revision": "harbor-agent-evidence-v1",
            "model": "fixture-model",
            "status": "failed",
            "tokens": {"input_tokens": 120, "cache_tokens": 40, "output_tokens": 30},
        }
        assert "langfuse.observation.model.name" not in span.attributes
        assert "langfuse.observation.usage_details" not in span.attributes
        assert "langfuse.observation.cost_details" not in span.attributes


def test_repeated_export_reuses_the_verified_receipt_without_another_trace(tmp_path):
    """Dropping receipt reuse would duplicate a deterministic attempt in Langfuse."""
    trial = copy_trial(tmp_path)
    first = FakeLangfuse()
    receipt = L.export_job_attempts(trial.parent, METADATA, client=first)
    must_not_export = FakeLangfuse(readback="absent")

    assert L.export_job_attempts(trial.parent, METADATA, client=must_not_export) == receipt
    assert must_not_export.observations == []
    assert must_not_export.flushes == 0 and must_not_export.reads == []


def test_production_readback_uses_parent_basic_auth_and_public_trace_endpoint(tmp_path, monkeypatch):
    """Wrong auth or endpoint would make an SDK enqueue look verified without public read-back."""
    trial = copy_trial(tmp_path)
    client = FakeSdkOnly()
    requests = []
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-fixture")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-fixture")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.fixture.invalid/")

    class Response:
        status_code = 200

        def json(self):
            return {
                "id": client.delegate.seeds[-1] and Langfuse.create_trace_id(
                    seed=client.delegate.seeds[-1]),
                "observations": [{"id": client.delegate.observation.id,
                                  "name": "harbor-attempt", "type": "AGENT",
                                  "metadata": client.observations[-1]["metadata"]}],
            }

    class MissingResponse:
        status_code = 404

    def fake_get(url, **kwargs):
        requests.append((url, kwargs))
        return Response() if client.observations else MissingResponse()

    monkeypatch.setattr(L.httpx, "get", fake_get)

    L.export_job_attempts(trial.parent, METADATA, client=client)

    trace_id = Langfuse.create_trace_id(seed=client.delegate.seeds[-1])
    assert requests == [
        (f"https://langfuse.fixture.invalid/api/public/traces/{trace_id}",
         {"auth": ("pk-fixture", "sk-fixture"), "timeout": 15}),
        (f"https://langfuse.fixture.invalid/api/public/traces/{trace_id}",
         {"auth": ("pk-fixture", "sk-fixture"), "timeout": 15}),
    ]


def test_production_readback_error_fails_before_emitting_an_observation(tmp_path, monkeypatch):
    """Only a confirmed missing deterministic trace permits a first observation."""
    trial = copy_trial(tmp_path)
    client = FakeSdkOnly()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-fixture")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-fixture")

    class UnauthorizedResponse:
        status_code = 401

    monkeypatch.setattr(L.httpx, "get", lambda *args, **kwargs: UnauthorizedResponse())

    with pytest.raises(L.TelemetryReceiptError, match="status 401"):
        L.export_job_attempts(trial.parent, METADATA, client=client)

    assert client.observations == []


@pytest.mark.parametrize("readback", ["absent", "wrong-id", "wrong-hash", "wrong-evidence"])
def test_unmatched_public_readback_never_creates_a_verified_receipt(tmp_path, readback):
    """Accepting absent or mismatched public readback would turn pending state into proof."""
    trial = copy_trial(tmp_path)

    with pytest.raises(L.TelemetryReceiptError, match="read-back"):
        L.export_job_attempts(trial.parent, METADATA, client=FakeLangfuse(readback=readback))

    receipt = json.loads((trial / "langfuse-receipt.json").read_text())
    assert receipt["status"] == "pending"


def test_failed_attempt_is_traced_and_telemetry_failure_does_not_mutate_harbor_result(tmp_path):
    """Failure telemetry must describe persisted evidence, never rewrite or rerun it."""
    trial = copy_trial(tmp_path)
    before = (trial / "result.json").read_bytes()
    client = FakeLangfuse(readback="absent")

    with pytest.raises(L.TelemetryReceiptError):
        L.export_job_attempts(trial.parent, METADATA, client=client)

    assert client.observations[0]["output"]["status"] == "failed"
    assert (trial / "result.json").read_bytes() == before
    assert json.loads((trial / "result.json").read_text())["verifier_result"]["rewards"] == {"reward": 1.0}


@pytest.mark.parametrize("state", [
    "empty", "withheld", "stale", "malformed", "empty-id", "whitespace-id", "wrong-id",
])
def test_receipt_validation_rejects_every_incomplete_telemetry_state(tmp_path, state):
    """No empty, withheld, stale, or malformed receipt may silently publish a matrix."""
    trial = copy_trial(tmp_path)
    receipt = trial / "langfuse-receipt.json"
    if state == "withheld":
        receipt.write_text(json.dumps({"status": "withheld"}))
    elif state == "stale":
        receipt.write_text(json.dumps({
            "status": "verified",
            "trace_id": "1234567890abcdef1234567890abcdef",
            "payload_sha256": "0" * 64,
            "exporter_revision": "harbor-langfuse-v1",
        }))
    elif state == "malformed":
        receipt.write_text("{not-json")
    elif state in {"empty-id", "whitespace-id", "wrong-id"}:
        payload = L.build_attempt_payload(trial, METADATA)
        receipt.write_text(json.dumps({
            "status": "verified",
            "trace_id": {"empty-id": "", "whitespace-id": "  ", "wrong-id": "0" * 32}[state],
            "payload_sha256": payload_sha256(payload),
            "exporter_revision": "harbor-langfuse-v1",
        }))

    with pytest.raises(L.TelemetryReceiptError, match="receipt"):
        L.validate_job_receipts(trial.parent, METADATA)


def test_receipt_validation_rejects_an_exact_provenance_free_receipt(tmp_path):
    """A matching old payload identity cannot authorize publication without provenance."""
    trial = copy_trial(tmp_path)
    payload = L.build_attempt_payload(trial, METADATA)
    digest = payload_sha256(payload)
    (trial / L.RECEIPT_NAME).write_text(json.dumps({
        "status": "verified",
        "trace_id": Langfuse.create_trace_id(seed=f"{L.EXPORTER_REVISION}:{digest}"),
        "payload_sha256": digest,
        "exporter_revision": L.EXPORTER_REVISION,
    }))

    with pytest.raises(L.TelemetryReceiptError, match="provenance"):
        L.validate_job_receipts(trial.parent, METADATA)


class DelayedReadbackLangfuse(FakeLangfuse):
    """First post-flush read misses; the next preflight sees the persisted observation."""

    def read_trace(self, trace_id: str):
        self.reads.append(trace_id)
        if not self.observations or (len(self.observations) == 1 and len(self.reads) < 3):
            return None
        metadata = dict(self.observations[0]["metadata"])
        return {
            "id": trace_id,
            "observations": [{"id": self.observation.id,
                              "name": "harbor-attempt", "type": "AGENT",
                              "metadata": metadata}],
        }


def test_post_flush_readback_polls_before_deferring_finalization(tmp_path, monkeypatch):
    """A short indexing lag must finalize in one exporter pass without a duplicate trace."""
    trial = copy_trial(tmp_path)
    client = DelayedReadbackLangfuse()
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)

    receipts = L.export_job_attempts(trial.parent, METADATA, client=client)

    assert len(receipts) == 1
    assert len(client.observations) == 1
    assert client.flushes == 1
    assert len(client.reads) == 3
    assert sleeps == [L._READBACK_POLL_SECONDS]


def test_repeated_404_after_unknown_dispatch_never_emits_a_second_observation(tmp_path):
    """A point-in-time missing trace cannot authorize another dispatch after flush."""
    trial = copy_trial(tmp_path)
    client = FakeLangfuse(readback="absent")

    for _attempt in range(2):
        with pytest.raises(L.TelemetryReceiptError):
            L.export_job_attempts(trial.parent, METADATA, client=client)

    pending = json.loads((trial / L.RECEIPT_NAME).read_text())
    assert pending["status"] == "pending"
    assert pending["observation_id"] == client.observation.id
    assert len(client.observations) == 1
    with pytest.raises(L.TelemetryReceiptError):
        L.validate_job_receipts(trial.parent, METADATA)


def test_unknown_pending_receipt_never_adopts_an_uncaptured_observation_id(tmp_path):
    """A crash before persisting the SDK ID leaves non-authorizing state, even after read-back."""
    trial = copy_trial(tmp_path)
    payload = L.build_attempt_payload(trial, METADATA)
    digest = payload_sha256(payload)
    evidence = L._telemetry_evidence(payload)
    (trial / L.RECEIPT_NAME).write_text(json.dumps(L._pending_receipt(digest)))

    class UnknownPendingLangfuse(FakeLangfuse):
        def read_trace(self, trace_id: str):
            return {"id": trace_id, "observations": [{
                "id": "2222222222222222",
                "name": "harbor-attempt",
                "type": "AGENT",
                "metadata": {
                    "payload_sha256": digest,
                    "exporter_revision": L.EXPORTER_REVISION,
                    "telemetry_evidence": evidence,
                },
            }]}

        def start_observation(self, **kwargs):
            pytest.fail("an unknown pending attempt must never emit")

    with pytest.raises(L.TelemetryReceiptError, match="pending"):
        L.export_job_attempts(trial.parent, METADATA, client=UnknownPendingLangfuse())

    assert json.loads((trial / L.RECEIPT_NAME).read_text()) == L._pending_receipt(digest)


def test_concurrent_receiptless_exporters_create_at_most_one_observation(tmp_path):
    """Atomic pending state serializes contenders that both observed the trace missing."""
    trial = copy_trial(tmp_path)

    class ConcurrentAbsentLangfuse(FakeLangfuse):
        def __init__(self):
            super().__init__(readback="absent")
            self.preflight = threading.Barrier(2)
            self.read_count = 0
            self.read_lock = threading.Lock()

        def read_trace(self, trace_id: str):
            with self.read_lock:
                self.read_count += 1
                count = self.read_count
            if count <= 2:
                self.preflight.wait(timeout=5)
            self.reads.append(trace_id)
            return None

    client = ConcurrentAbsentLangfuse()

    def export():
        with pytest.raises(L.TelemetryReceiptError):
            L.export_job_attempts(trial.parent, METADATA, client=client)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _index: export(), range(2)))

    pending = json.loads((trial / L.RECEIPT_NAME).read_text())
    assert pending["status"] == "pending"
    assert pending["observation_id"] == client.observation.id
    assert len(client.observations) == 1


def test_duplicate_preexisting_observations_fail_closed_without_another_emission(tmp_path):
    """A trace with duplicate matching observations is not one verified attempt."""
    trial = copy_trial(tmp_path)
    payload = L.build_attempt_payload(trial, METADATA)
    digest = payload_sha256(payload)

    class DuplicateLangfuse(FakeLangfuse):
        def read_trace(self, trace_id: str):
            metadata = {"payload_sha256": digest, "exporter_revision": L.EXPORTER_REVISION}
            observation = {"id": "1111111111111111",
                           "name": "harbor-attempt", "type": "AGENT", "metadata": metadata}
            return {"id": trace_id, "observations": [observation, observation]}

        def start_observation(self, **kwargs):
            pytest.fail("duplicate preexisting trace must not emit")

    with pytest.raises(L.TelemetryReceiptError, match="existing"):
        L.export_job_attempts(trial.parent, METADATA, client=DuplicateLangfuse())


def test_evidence_root_exports_preserved_attempts_without_rerunning_agents(tmp_path):
    """Changing recursive evidence discovery must not orphan preserved proprietary attempts."""
    trial = copy_trial(tmp_path)
    combo = trial.parents[1]
    (combo / "combo.json").write_text(json.dumps(PROVENANCE_METADATA))
    (trial / "result.json").unlink()
    client = FakeLangfuse()

    receipts = L.export_evidence_root(tmp_path, client=client)

    assert len(receipts) == 1
    assert (trial / "langfuse-receipt.json").is_file()
    assert len(client.observations) == 1
    assert client.observations[0]["input"] == {
        "name": "fixture-h0", "checksum": "", "source": "",
        "text": "Use only deterministic fixture input.",
    }
    assert client.observations[0]["output"]["skill"] == "demo"
    assert client.observations[0]["output"]["timestamps"] == {
        "started_at": "2000-01-01T00:00:00Z",
        "finished_at": "2000-01-01T00:00:01Z",
    }


def test_evidence_root_refuses_missing_explicit_provenance_before_emission(tmp_path):
    """Preserved evidence cannot infer skill or task text from directory names."""
    trial = copy_trial(tmp_path)
    (trial.parents[1] / "combo.json").write_text(json.dumps(METADATA))
    client = FakeLangfuse()

    with pytest.raises(L.TelemetryReceiptError, match="provenance"):
        L.export_evidence_root(tmp_path, client=client)

    assert client.observations == []
    assert not (trial / L.RECEIPT_NAME).exists()


def test_export_discovers_a_verifier_only_failed_attempt(tmp_path):
    """A failed trial that retained only verifier evidence is still one persisted attempt."""
    trial = tmp_path / "job" / "fixture-h1__attempt-2"
    solution = trial / "verifier" / "solution"
    solution.mkdir(parents=True)
    (solution / "answer.txt").write_text("partial fixture answer")
    client = FakeLangfuse()

    receipts = L.export_job_attempts(trial.parent, METADATA, client=client)

    assert len(receipts) == 1 and len(client.observations) == 1
    assert client.observations[0]["input"]["name"] == "fixture-h1"
    assert (trial / "langfuse-receipt.json").is_file()


def test_cli_exports_each_repeated_evidence_root(tmp_path, monkeypatch):
    """Dropping repeated roots would leave part of the preserved evidence unreceipted."""
    first, second = tmp_path / "first", tmp_path / "second"
    first_metadata, second_metadata = tmp_path / "first.json", tmp_path / "second.json"
    first_metadata.write_text(json.dumps(PROVENANCE_METADATA))
    second_metadata.write_text(json.dumps(PROVENANCE_METADATA))
    calls = []
    monkeypatch.setattr(
        L, "export_evidence_root",
        lambda root, metadata=None: calls.append((root, metadata)) or [],
    )

    assert L.main([
        "--root", str(first), "--metadata", str(first_metadata),
        "--root", str(second), "--metadata", str(second_metadata),
    ]) == 0
    assert calls == [(first, PROVENANCE_METADATA), (second, PROVENANCE_METADATA)]


def test_cli_refuses_a_preserved_root_without_paired_migration_metadata(tmp_path):
    """A root and its migration document are an explicit one-to-one input."""
    with pytest.raises(SystemExit):
        L.main(["--root", str(tmp_path / "preserved")])


def test_export_job_attempts_filters_native_arm_identity(tmp_path):
    from ingot.optimize.harbor_native import NativeTrialIdentity, identity_env

    skill_trial = copy_trial(tmp_path)
    control_trial = tmp_path / "job" / "fixture-h0__attempt-2"
    shutil.copytree(FIXTURE, control_trial)
    common = dict(combination_id="codex@fixture-model--dell-fixture-ffffffffffff",
                  endpoint_fingerprint="ffffffffffff", harness="codex",
                  protocol="responses", gateway_revision="direct")
    skill = NativeTrialIdentity(**common, arm="skill")
    control = NativeTrialIdentity(**common, arm="control")
    (skill_trial / "lock.json").write_text(json.dumps({"agent": {"env": identity_env(skill)}}))
    (control_trial / "lock.json").write_text(json.dumps({"agent": {"env": identity_env(control)}}))
    client = FakeLangfuse()

    receipts = L.export_job_attempts(skill_trial.parent, METADATA, identity=skill, client=client)

    assert len(receipts) == 1
    assert (skill_trial / L.RECEIPT_NAME).is_file()
    assert not (control_trial / L.RECEIPT_NAME).exists()


def test_identity_filter_skips_unrelated_rejected_artifacts_before_opening_them(tmp_path):
    from ingot.optimize.harbor_native import NativeTrialIdentity, identity_env

    selected = copy_trial(tmp_path)
    other = tmp_path / "job" / "fixture-h0__attempt-2"
    shutil.copytree(FIXTURE, other)
    common = dict(combination_id="codex@fixture-model--dell-fixture-ffffffffffff",
                  endpoint_fingerprint="ffffffffffff", harness="codex",
                  protocol="responses", gateway_revision="direct")
    skill = NativeTrialIdentity(**common, arm="skill")
    control = NativeTrialIdentity(**common, arm="control")
    (selected / "lock.json").write_text(json.dumps({"agent": {"env": identity_env(skill)}}))
    (other / "lock.json").write_text(json.dumps({"agent": {"env": identity_env(control)}}))
    shutil.rmtree(other / "verifier")
    (other / "verifier").symlink_to(tmp_path, target_is_directory=True)

    attempts = L._job_attempts(selected.parent, skill)

    assert attempts == [selected]
