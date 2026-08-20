import json
from pathlib import Path

import pytest

from ingot.optimize import harbor_eval as H
from ingot.optimize.harbor_native import NativeTrialIdentity, identity_env
from ingot.optimize.harbor_targets import LocalTarget


def _identity(arm="skill"):
    return NativeTrialIdentity(
        combination_id="aider@dot-backbone--dell-qwen-deadbeefcafe",
        endpoint_fingerprint="deadbeefcafe", harness="aider", protocol="chat",
        gateway_revision="direct", arm=arm)


def _attempt(job: Path, name: str, identity: NativeTrialIdentity):
    trial = job / name
    trial.mkdir(parents=True)
    (trial / "lock.json").write_text(json.dumps({"agent": {"env": identity_env(identity)}}))
    (trial / "result.json").write_text(json.dumps({"task_name": "ingot/h1",
                                                    "finished_at": "2026-08-13T00:00:00Z"}))


def test_watch_native_job_releases_each_identity_once_at_exact_cardinality(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    skill, control = _identity("skill"), _identity("control")
    seen = []

    _attempt(job, "h1__s1", skill)
    expected = {skill: {"h1": 2}, control: {"h1": 1}}
    assert H.watch_native_job(job, expected, seen.append) == set()
    _attempt(job, "h1__c1", control)
    assert H.watch_native_job(job, expected, seen.append) == {control}
    _attempt(job, "h1__s2", skill)
    assert H.watch_native_job(job, expected, seen.append,
                              released={control}) == {skill, control}
    assert seen == [control, skill]


def test_watch_native_job_refuses_more_attempts_than_contract(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    identity = _identity()
    _attempt(job, "h1__one", identity)
    _attempt(job, "h1__two", identity)

    with pytest.raises(RuntimeError, match="exceeded expected attempts"):
        H.watch_native_job(job, {identity: {"h1": 1}}, lambda _identity: None)


def test_run_native_job_uses_one_harbor_process_and_progress_callback(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text("{}")
    jobs = tmp_path / "jobs"
    identity = _identity()
    calls = []

    class Process:
        returncode = 0
        count = 0
        pid = 4242

        def poll(self):
            self.count += 1
            if self.count == 1:
                _attempt(jobs / "full", "h1__one", identity)
                return None
            return 0

        def wait(self):
            return 0

    monkeypatch.setattr(H.subprocess, "Popen", lambda argv, **kwargs:
                        calls.append((argv, kwargs)) or Process())
    monkeypatch.setattr(H, "_process_start_token", lambda pid: f"start-{pid}")
    monkeypatch.setattr(H.time, "sleep", lambda _seconds: None)

    overlay = tmp_path / "network.compose.yml"
    overlay.write_text("networks: {}")
    result = H.run_native_job(
        config, jobs, "full", {identity: {"h1": 1}},
        on_ready=lambda item: calls.append(item),
        process_env={"PATH": "/bin", "HARBOR_EXTRA_DOCKER_COMPOSE": str(overlay)},
    )

    assert result == jobs / "full"
    assert len([item for item in calls if isinstance(item, tuple)]) == 1
    argv, kwargs = calls[0]
    assert argv[:3] == [H.HARBOR_BIN, "run", "--config"]
    assert argv[-6:] == ["--override-memory-mb", "2048", "--job-name", "full",
                         "--extra-docker-compose", str(overlay)]
    assert kwargs["env"]["PATH"] == "/bin"
    assert "HARBOR_EXTRA_DOCKER_COMPOSE" not in kwargs["env"]
    assert kwargs["env"]["OPENAI_API_KEY"] == "local"
    assert kwargs["env"]["ANTHROPIC_API_KEY"] == "local"
    assert kwargs["env"]["CODEX_API_KEY"] == "local"
    assert calls[-1] == identity


def test_run_native_job_does_not_respawn_harbor_for_released_terminal_evidence(
        tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text("{}")
    jobs = tmp_path / "jobs"
    job = jobs / "full"
    identity = _identity()
    _attempt(job, "h1__one", identity)
    jobs.mkdir(exist_ok=True)
    (jobs / "full.released.json").write_text(json.dumps({
        "released": [identity_env(identity)],
    }))
    monkeypatch.setattr(H, "_process_start_token", lambda pid: f"start-{pid}")
    monkeypatch.setattr(H.subprocess, "Popen",
                        lambda *_args, **_kwargs: pytest.fail("Harbor was respawned"))

    result = H.run_native_job(
        config, jobs, "full", {identity: {"h1": 1}},
        on_ready=lambda _item: pytest.fail("released callback repeated"),
        process_env={"PATH": "/bin"}, allow_completed_reuse=True,
    )

    assert result == job
    assert not (jobs / "full.owner.json").exists()


def test_run_native_job_refinalizes_terminal_evidence_without_respawning_harbor(
        tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text("{}")
    jobs = tmp_path / "jobs"
    job = jobs / "full"
    identity = _identity()
    _attempt(job, "h1__one", identity)
    seen = []
    monkeypatch.setattr(H, "_process_start_token", lambda pid: f"start-{pid}")
    monkeypatch.setattr(H.subprocess, "Popen",
                        lambda *_args, **_kwargs: pytest.fail("Harbor was respawned"))

    result = H.run_native_job(
        config, jobs, "full", {identity: {"h1": 1}}, on_ready=seen.append,
        process_env={"PATH": "/bin"}, allow_completed_reuse=True,
    )

    assert result == job
    assert seen == [identity]
    assert json.loads((jobs / "full.released.json").read_text()) == {
        "released": [identity_env(identity)],
    }
    assert not (jobs / "full.owner.json").exists()


def test_run_native_job_keeps_terminal_finalization_pending_without_respawning_harbor(
        tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text("{}")
    jobs = tmp_path / "jobs"
    job = jobs / "full"
    identity = _identity()
    _attempt(job, "h1__one", identity)
    monkeypatch.setattr(H, "_process_start_token", lambda pid: f"start-{pid}")
    monkeypatch.setattr(H.subprocess, "Popen",
                        lambda *_args, **_kwargs: pytest.fail("Harbor was respawned"))

    with pytest.raises(RuntimeError, match="native Harbor finalization is pending"):
        H.run_native_job(
            config, jobs, "full", {identity: {"h1": 1}}, on_ready=lambda _item: False,
            process_env={"PATH": "/bin"}, allow_completed_reuse=True,
        )

    assert json.loads((jobs / "full.released.json").read_text()) == {"released": []}
    assert not (jobs / "full.owner.json").exists()


def test_run_native_job_does_not_reuse_released_evidence_without_catalog_proof(
        tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text("{}")
    jobs = tmp_path / "jobs"
    job = jobs / "full"
    identity = _identity()
    _attempt(job, "h1__one", identity)
    jobs.mkdir(exist_ok=True)
    (jobs / "full.released.json").write_text(json.dumps({
        "released": [identity_env(identity)],
    }))
    calls = []

    class Process:
        def poll(self):
            return 0

        def wait(self):
            return 0

    monkeypatch.setattr(H, "_process_start_token", lambda pid: f"start-{pid}")
    monkeypatch.setattr(H.subprocess, "Popen",
                        lambda *_args, **_kwargs: calls.append(True) or Process())

    H.run_native_job(config, jobs, "full", {identity: {"h1": 1}},
                     on_ready=lambda _item: None, process_env={"PATH": "/bin"})

    assert calls == [True]


def test_watch_native_job_does_not_release_started_trial(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    identity = _identity()
    _attempt(job, "h1__one", identity)
    result = job / "h1__one" / "result.json"
    result.write_text(json.dumps({"task_name": "ingot/h1", "finished_at": None}))

    assert H.watch_native_job(job, {identity: {"h1": 1}}, lambda _item: None) == set()


def _target(alias="dell-qwen", model="dot-backbone", port=8011):
    return LocalTarget(alias=alias, display_name=model, base_url=f"http://host:{port}",
                       served_model=model, context_length=163840,
                       protocols=frozenset({"chat", "responses", "messages"}))


def test_native_sweep_runs_one_canary_queue_and_independent_caps(tmp_path, monkeypatch):
    first = _target()
    second = _target("spark-deepseek", "deepseek-v4-flash", 8899)
    monkeypatch.setattr(H, "HARBOR_DIR", tmp_path / "harbor")
    monkeypatch.setattr(H, "load_tasks", lambda _skill: ([], [{"task": "x"}] * 4, {}))
    monkeypatch.setattr(H, "stage_skill", lambda _skill: tmp_path / "source")
    monkeypatch.setattr(H, "build_dataset", lambda *_args: tmp_path / "dataset")
    monkeypatch.setattr(H.shutil, "which", lambda _binary: "/bin/harbor")
    monkeypatch.setattr(H, "discover_target", lambda alias, _url: first if alias == first.alias else second)
    monkeypatch.setattr(H, "probe_protocol", lambda *_args: None)
    monkeypatch.setattr(H, "probe_chat_tool_round_trip", lambda *_args: None)
    monkeypatch.setattr(H, "run_canary", lambda *_args, **_kwargs:
                        pytest.fail("serial canary ran"))
    calls = []

    def canaries(*args, **kwargs):
        calls.append(("canary", args, kwargs))
        return {f"aider@{target.served_model}--{target.job_slug}": {"ok": True}
                for target in (first, second)}

    monkeypatch.setattr(H, "_run_native_canaries", canaries, raising=False)
    monkeypatch.setattr(H, "_run_native_full_arms",
                        lambda *args, **kwargs: calls.append(("full", args, kwargs)))

    H.run_local_sweep("demo", [first, second], harnesses=("aider",), native_parallel=True,
                      global_concurrency=32, endpoint_concurrency=6, log=lambda *_args: None)

    assert [item[0] for item in calls] == ["canary", "full"]
    assert calls[0][2]["global_limit"] == 32
    assert calls[0][2]["endpoint_limit"] == 6
    assert calls[1][2]["global_limit"] == 32
    assert calls[1][2]["endpoint_limit"] == 6


def test_native_canary_restart_rehydrates_terminal_identity_without_agent_rerun(tmp_path,
                                                                                monkeypatch):
    target = _target()
    identity = H.native_trial_identity(target, "aider", "canary")
    job = tmp_path / "canaries" / "demo" / "native-canaries"
    trial = job / "demo-h0__one"
    solution = trial / "verifier" / "solution"
    solution.mkdir(parents=True)
    (solution / "answer.md").write_text("done")
    (trial / "lock.json").write_text(json.dumps({"agent": {"env": identity_env(identity)}}))
    (trial / "result.json").write_text(json.dumps({"task_name": "ingot/demo-h0",
                                                    "finished_at": "now"}))
    monkeypatch.setattr(H, "compile_canary_job", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(H, "write_job_config", lambda *_args: None)
    monkeypatch.setattr(H, "_telemetry_provenance", lambda *_args: {})
    exported = []
    monkeypatch.setattr(H, "export_job_attempts", lambda *args, **kwargs: exported.append(kwargs["identity"]))
    monkeypatch.setattr(H, "run_native_job", lambda *_args, **_kwargs: job)

    records = H._run_native_canaries(
        "demo", tmp_path / "dataset", [target], ("aider",), [{"task": "x"}] * 4,
        str(tmp_path / "source"), tmp_path / "canaries", global_limit=16,
        endpoint_limit=4)

    assert records[identity.combination_id]["ok"] is True
    assert exported == [identity]


def test_native_canary_telemetry_failure_is_unmeasured(tmp_path, monkeypatch):
    target = _target()
    identity = H.native_trial_identity(target, "aider", "canary")
    monkeypatch.setattr(H, "compile_canary_job", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(H, "write_job_config", lambda *_args: None)
    monkeypatch.setattr(H, "_telemetry_provenance", lambda *_args: {})
    monkeypatch.setattr(H, "export_job_attempts",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("telemetry")))

    callback_results = []

    def native(_config, jobs_root, job_name, expected, *, on_ready, **_kwargs):
        for item in expected:
            callback_results.append(on_ready(item))
        return jobs_root / job_name

    monkeypatch.setattr(H, "run_native_job", native)
    records = H._run_native_canaries(
        "demo", tmp_path / "dataset", [target], ("aider",), [{"task": "x"}] * 4,
        str(tmp_path / "source"), tmp_path / "canaries", global_limit=16,
        endpoint_limit=4)
    record = records[identity.combination_id]
    assert callback_results == [False]
    assert "ok" not in record
    assert record["error"] == "canary telemetry receipt was not verified"


def test_native_completed_restart_publishes_failed_canary_without_callbacks(tmp_path, monkeypatch):
    from ingot.optimize import harbor_rescore as R

    passed = _target()
    failed = _target("spark-deepseek", "deepseek-v4-flash", 8899)
    holdout = [{"task": f"task {index}"} for index in range(4)]
    source = tmp_path / "source"
    skill_dir = source / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("fixture skill\n")
    scoring = {
        "judge": "agy/fixture",
        "scoring_revision": "fixture-v1",
        "judge_billing_mode": "subscription",
        "judge_runtime": "fixture",
    }
    monkeypatch.setattr(R, "current_scoring_identity", lambda: scoring)
    monkeypatch.setattr(H, "compile_measurement_job", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(H, "_process_start_token", lambda pid: f"start-{pid}")
    monkeypatch.setattr(H, "export_job_attempts", lambda *_args, **_kwargs:
                        pytest.fail("completed restart repeated telemetry export"))
    monkeypatch.setattr(H.subprocess, "Popen", lambda *_args, **_kwargs:
                        pytest.fail("completed restart respawned Harbor"))
    rescored = []

    def fake_rescore(_skill, **kwargs):
        rescored.append(list(kwargs["combination_paths"]))

    monkeypatch.setattr(R, "rescore", fake_rescore)
    manifest = {"combinations": {}}
    jobs_root = tmp_path / "jobs"
    cell = H.NativeCell(passed, "aider")
    job_name = H._native_full_job_name(cell)
    job = jobs_root / job_name
    output_root = tmp_path / "published"
    output_root.mkdir()
    passed_id = f"aider@{passed.served_model}--{passed.job_slug}"
    failed_id = f"aider@{failed.served_model}--{failed.job_slug}"
    skill_identity = H.native_trial_identity(passed, "aider", "skill")
    control_identity = H.native_trial_identity(passed, "aider", "control")
    provenance = H._telemetry_provenance("demo", holdout, str(source))
    (output_root / "demo.rescored.json").write_text(json.dumps({"combinations": {
        passed_id: {
            "combination": passed_id,
            "endpoint_fingerprint": skill_identity.endpoint_fingerprint,
            "skill_sha256": provenance["skill_sha256"],
            "task_fingerprint": H._task_fingerprint(holdout),
            "attempts": 3,
            "harness": "aider",
            "protocol": skill_identity.protocol,
            "gateway_revision": skill_identity.gateway_revision,
            **scoring,
            "lift": 0.1,
        },
    }}))
    for identity in (skill_identity, control_identity):
        for task_index in range(4):
            for attempt in range(3):
                trial = job / f"{identity.arm}-h{task_index}-a{attempt}"
                trial.mkdir(parents=True)
                (trial / "lock.json").write_text(json.dumps({
                    "agent": {"env": identity_env(identity)},
                }))
                (trial / "result.json").write_text(json.dumps({
                    "task_name": f"ingot/demo-h{task_index}",
                    "finished_at": "2026-08-14T00:00:00Z",
                }))
    (jobs_root / f"{job_name}.released.json").write_text(json.dumps({
        "released": [identity_env(skill_identity), identity_env(control_identity)],
    }))
    (jobs_root / "native-full.pipeline.json").write_text(json.dumps({
        "agent_identity": {
            "skill_sha256": provenance["skill_sha256"],
            "task_fingerprint": H._task_fingerprint(holdout),
            "attempts": 3,
            "exporter_revision": H.EXPORTER_REVISION,
            "cells": [[passed_id, skill_identity.gateway_revision]],
        },
        "scoring_identity": scoring,
        "exported": {passed_id: ["control", "skill"]},
        "graded": [passed_id],
        "published": [passed_id],
    }))

    H._run_native_full_arms(
        "demo", tmp_path / "dataset", [passed, failed], ("aider",), holdout, str(source),
        jobs_root, manifest, {
            passed_id: {"ok": True},
            failed_id: {"error": "adapter did not route"},
        },
        global_limit=16, endpoint_limit=4, publish_root=output_root,
        allow_completed_reuse=True,
    )

    assert [[path.name for path in paths] for paths in rescored] == [[failed_id]]
    metadata = json.loads((jobs_root / failed_id / "combo.json").read_text())
    assert metadata["canary_error"] == "adapter did not route"
    assert metadata["skill_sha256"]


def test_prior_lift_for_now_failed_cell_waits_for_selected_measurement(tmp_path, monkeypatch):
    from ingot.optimize import harbor_rescore as R

    failed = _target()
    passed = _target("spark-deepseek", "deepseek-v4-flash", 8899)
    holdout = [{"task": f"task {index}"} for index in range(4)]
    source = tmp_path / "source"
    skill_dir = source / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("fixture skill\n")
    scoring = {
        "judge": "agy/fixture",
        "scoring_revision": "fixture-v1",
        "judge_billing_mode": "subscription",
        "judge_runtime": "fixture",
    }
    monkeypatch.setattr(R, "current_scoring_identity", lambda: scoring)
    monkeypatch.setattr(H, "compile_measurement_job", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(H, "write_job_config", lambda *_args, **_kwargs: None)
    exported = []
    monkeypatch.setattr(H, "export_job_attempts",
                        lambda *_args, **kwargs: exported.append(kwargs["identity"]))
    rescored = []

    def fake_rescore(_skill, **kwargs):
        rescored.append(list(kwargs["combination_paths"]))

    monkeypatch.setattr(R, "rescore", fake_rescore)

    def fake_run_native_job(_config, _jobs_root, _job_name, expected, *, on_ready, **_kwargs):
        for identity in expected:
            assert identity.combination_id == passed_id
            assert on_ready(identity) is True

    monkeypatch.setattr(H, "run_native_job", fake_run_native_job)
    manifest = {"combinations": {}}
    jobs_root = tmp_path / "jobs"
    output_root = tmp_path / "published"
    output_root.mkdir()
    failed_id = f"aider@{failed.served_model}--{failed.job_slug}"
    passed_id = f"aider@{passed.served_model}--{passed.job_slug}"
    failed_identity = H.native_trial_identity(failed, "aider", "skill")
    provenance = H._telemetry_provenance("demo", holdout, str(source))
    (output_root / "demo.rescored.json").write_text(json.dumps({"combinations": {
        failed_id: {
            "combination": failed_id,
            "endpoint_fingerprint": failed_identity.endpoint_fingerprint,
            "skill_sha256": provenance["skill_sha256"],
            "task_fingerprint": H._task_fingerprint(holdout),
            "attempts": 3,
            "harness": "aider",
            "protocol": failed_identity.protocol,
            "gateway_revision": failed_identity.gateway_revision,
            **scoring,
            "lift": 0.1,
        },
    }}))

    H._run_native_full_arms(
        "demo", tmp_path / "dataset", [failed, passed], ("aider",), holdout, str(source),
        jobs_root, manifest, {
            failed_id: {"error": "adapter did not route"},
            passed_id: {"ok": True},
        },
        global_limit=16, endpoint_limit=4, publish_root=output_root,
    )

    assert [[path.name for path in paths] for paths in rescored] == [[failed_id, passed_id]]
    assert {identity.combination_id for identity in exported} == {passed_id}


def test_native_full_arms_runs_stable_cells_concurrently_instead_of_one_wide_job(
        tmp_path, monkeypatch):
    """A partial run must finish cells, not spread work across the whole matrix first."""
    import threading
    from ingot.optimize import harbor_rescore as R

    first = _target()
    second = _target("spark-deepseek", "deepseek-v4-flash", 8899)
    holdout = [{"task": f"task {index}"} for index in range(4)]
    source = tmp_path / "source"
    skill_dir = source / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("fixture skill\n")
    monkeypatch.setattr(R, "current_scoring_identity", lambda: {
        "judge": "agy/fixture", "scoring_revision": "fixture-v1",
        "judge_billing_mode": "subscription", "judge_runtime": "fixture",
    })
    monkeypatch.setattr(R, "rescore", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(H, "write_job_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(H, "export_job_attempts", lambda *_args, **_kwargs: None)
    compiled = []

    def compile_job(_dataset, _tasks, cells, *_args, **_kwargs):
        compiled.append([cell.combination_id for cell in cells])
        return {}

    monkeypatch.setattr(H, "compile_measurement_job", compile_job)
    barrier = threading.Barrier(2)
    calls = []

    def native(_config, jobs_root, job_name, expected, *, on_ready, **_kwargs):
        calls.append((job_name, threading.get_ident(), set(expected)))
        barrier.wait(timeout=2)
        for identity in expected:
            assert on_ready(identity) is True
        return jobs_root / job_name

    monkeypatch.setattr(H, "run_native_job", native)
    ids = [f"aider@{target.served_model}--{target.job_slug}" for target in (first, second)]

    H._run_native_full_arms(
        "demo", tmp_path / "dataset", [first, second], ("aider",), holdout, str(source),
        tmp_path / "jobs", {"combinations": {}}, {key: {"ok": True} for key in ids},
        global_limit=12, endpoint_limit=6, publish_root=tmp_path / "published",
    )

    assert compiled == [[ids[0]], [ids[1]]]
    assert len(calls) == 2
    assert len({thread_id for _name, thread_id, _expected in calls}) == 2
    assert all(len(expected) == 2 for _name, _thread_id, expected in calls)
    assert len({name for name, _thread_id, _expected in calls}) == 2


def test_native_full_arms_never_overlaps_jobs_for_one_endpoint(tmp_path, monkeypatch):
    import threading
    from ingot.optimize import harbor_rescore as R

    target = _target()
    holdout = [{"task": f"task {index}"} for index in range(4)]
    source = tmp_path / "source"
    skill_dir = source / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("fixture skill\n")
    monkeypatch.setattr(R, "current_scoring_identity", lambda: {
        "judge": "agy/fixture", "scoring_revision": "fixture-v1",
        "judge_billing_mode": "subscription", "judge_runtime": "fixture",
    })
    monkeypatch.setattr(R, "rescore", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(H, "compile_measurement_job", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(H, "write_job_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(H, "export_job_attempts", lambda *_args, **_kwargs: None)
    guard = threading.Lock()
    second_entered = threading.Event()
    active = 0
    peak = 0

    def native(_config, jobs_root, job_name, expected, *, on_ready, **_kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
            first = active == 1 and peak == 1
            if active == 2:
                second_entered.set()
        try:
            if first:
                second_entered.wait(timeout=0.2)
            for identity in expected:
                assert on_ready(identity) is True
        finally:
            with guard:
                active -= 1
        return jobs_root / job_name

    monkeypatch.setattr(H, "run_native_job", native)
    ids = [f"{harness}@{target.served_model}--{target.job_slug}"
           for harness in ("aider", "pi")]

    H._run_native_full_arms(
        "demo", tmp_path / "dataset", [target], ("aider", "pi"), holdout, str(source),
        tmp_path / "jobs", {"combinations": {}}, {key: {"ok": True} for key in ids},
        global_limit=12, endpoint_limit=6, publish_root=tmp_path / "published",
    )

    assert peak == 1


def test_native_full_arms_adopts_legacy_identity_and_runs_only_missing_arm(tmp_path, monkeypatch):
    from ingot.optimize import harbor_rescore as R

    target = _target()
    holdout = [{"task": f"task {index}"} for index in range(4)]
    source = tmp_path / "source"
    skill_dir = source / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("fixture skill\n")
    monkeypatch.setattr(R, "current_scoring_identity", lambda: {
        "judge": "agy/fixture", "scoring_revision": "fixture-v1",
        "judge_billing_mode": "subscription", "judge_runtime": "fixture",
    })
    rescored = []
    monkeypatch.setattr(R, "rescore",
                        lambda *_args, **kwargs: rescored.append(kwargs["combination_paths"]))
    compiled_arms = []

    def compile_job(*_args, **kwargs):
        compiled_arms.append(tuple(kwargs["arms"]))
        return {}

    monkeypatch.setattr(H, "compile_measurement_job", compile_job)
    monkeypatch.setattr(H, "write_job_config", lambda *_args, **_kwargs: None)
    jobs_root = tmp_path / "jobs"
    cell = H.NativeCell(target, "aider")
    skill_identity = H.native_trial_identity(target, "aider", "skill")
    control_identity = H.native_trial_identity(target, "aider", "control")
    legacy = jobs_root / "native-full"
    for task_index in range(4):
        for attempt in range(3):
            trial = legacy / f"skill-h{task_index}-a{attempt}"
            trial.mkdir(parents=True)
            (trial / "lock.json").write_text(json.dumps({
                "agent": {"env": identity_env(skill_identity)},
            }))
            (trial / "result.json").write_text(json.dumps({
                "task_name": f"ingot/demo-h{task_index}",
                "finished_at": "2026-08-14T00:00:00Z",
            }))
    exports = []

    def export(job, _metadata, *, identity):
        exports.append((job.name, identity.arm))

    monkeypatch.setattr(H, "export_job_attempts", export)

    def native(_config, _root, _name, expected, *, on_ready, **_kwargs):
        assert set(expected) == {control_identity}
        assert on_ready(control_identity) is True

    monkeypatch.setattr(H, "run_native_job", native)
    combination = cell.combination_id

    H._run_native_full_arms(
        "demo", tmp_path / "dataset", [target], ("aider",), holdout, str(source),
        jobs_root, {"combinations": {}}, {combination: {"ok": True}},
        global_limit=8, endpoint_limit=4, publish_root=tmp_path / "published",
    )

    assert compiled_arms == [("control",)]
    assert exports == [("native-full", "skill"), (H._native_full_job_name(cell), "control")]
    assert len(rescored) == 1
    metadata = json.loads((jobs_root / combination / "combo.json").read_text())
    assert metadata["native_jobs"] == {
        "skill": "native-full", "control": H._native_full_job_name(cell),
    }
