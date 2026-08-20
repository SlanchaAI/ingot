from __future__ import annotations

import json
from pathlib import Path

import pytest

import ingot.optimize.harbor_catalog as HC
from ingot.optimize.harbor_catalog import CatalogIntent, enqueue_catalog, run_catalog

_REAL_PREPARE_EXECUTION = HC._prepare_execution
_REAL_INTENT_FOR_SKILL = HC._intent_for_skill


def _intent(skill: str = "demo", sha: str = "a" * 64, *, priority: int = 100,
            publish_root: str = "/srv/ingot/runs/harbor") -> CatalogIntent:
    return CatalogIntent(
        skill=skill,
        skill_sha256=sha,
        task_fingerprint="b" * 64,
        target_specs=("dell-qwen=http://127.0.0.1:8011",),
        target_fingerprints=("434045372e7c",),
        harnesses=("aider", "pi"),
        runtime_revisions=(("harbor", "0.20.0"), ("runner", "native-v1")),
        publish_root=publish_root,
        priority=priority,
    )


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.fixture(autouse=True)
def _catalog_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(HC, "CATALOG_OWNER", tmp_path / "global-controller.lock")
    monkeypatch.setattr(HC, "_prepare_execution",
                        lambda root, intent: (root / "source", root / "runs" / intent.digest))
    monkeypatch.setattr(HC, "_intent_for_skill",
                        lambda skill, targets, harnesses, priority=100, **_kwargs:
                        _intent(skill, priority=priority))
    monkeypatch.setattr(HC, "_refuse_live_harbor", lambda _root: None)


def test_enqueue_uses_content_identity_and_leaves_compatible_completion_untouched(tmp_path):
    intent = _intent()
    [path] = enqueue_catalog(tmp_path, [intent])
    assert path.name == f"{intent.digest}.json"
    assert _read(path)["identity"] == intent.identity_payload()
    state = tmp_path / "state" / path.name
    state.write_text(json.dumps({"schema": 1, "status": "complete", "priority": 100,
                                 "intent_digest": intent.digest, "marker": "keep"}))

    [again] = enqueue_catalog(tmp_path, [intent])

    assert again == path
    assert _read(state)["marker"] == "keep"


def test_enqueue_prioritizes_changed_then_incomplete_current_revision(tmp_path):
    old = _intent(sha="a" * 64)
    [old_path] = enqueue_catalog(tmp_path, [old])
    old_state = tmp_path / "state" / old_path.name
    old_state.write_text(json.dumps({"schema": 1, "status": "complete", "priority": 100,
                                     "intent_digest": old.digest}))

    changed = _intent(sha="c" * 64)
    [changed_path] = enqueue_catalog(tmp_path, [changed])
    assert _read(tmp_path / "state" / changed_path.name)["priority"] == 300

    current = _intent(skill="other")
    [current_path] = enqueue_catalog(tmp_path, [current])
    current_state = tmp_path / "state" / current_path.name
    state = _read(current_state)
    state["status"] = "running"
    current_state.write_text(json.dumps(state))
    enqueue_catalog(tmp_path, [current])
    assert _read(current_state)["priority"] == 200


def test_enqueue_reactivates_a_superseded_identity_that_becomes_current_again(tmp_path):
    intent = _intent()
    [intent_path] = enqueue_catalog(tmp_path, [intent])
    state_path = tmp_path / "state" / intent_path.name
    state = _read(state_path)
    state.update(status="superseded", error="IdentityChanged", finished_at=123.0)
    state_path.write_text(json.dumps(state))

    enqueue_catalog(tmp_path, [intent])

    assert _read(state_path) == {
        "schema": 1,
        "intent_digest": intent.digest,
        "status": "pending",
        "priority": 200,
    }


def test_stop_file_prevents_runner_and_preserves_pending(tmp_path):
    [path] = enqueue_catalog(tmp_path, [_intent()])
    stop = tmp_path / "STOP"
    stop.touch()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(HC, "run_local_sweep",
                        lambda *_args, **_kwargs: pytest.fail("runner called"))
    run_catalog(tmp_path, stop_file=stop)
    monkeypatch.undo()

    assert _read(tmp_path / "state" / path.name)["status"] == "pending"


def test_catalog_resumes_running_intent_and_advances_without_recreating_completion(tmp_path,
                                                                                   monkeypatch):
    first, second = _intent("first", priority=200), _intent("second", priority=100)
    first_path, second_path = enqueue_catalog(tmp_path, [first, second])
    first_state = tmp_path / "state" / first_path.name
    state = _read(first_state)
    state.update(status="running", run_root="same-root")
    first_state.write_text(json.dumps(state))
    calls = []

    def runner(skill, targets, **kwargs):
        calls.append((skill, tuple(target.alias for target in targets), kwargs["native_parallel"],
                      kwargs["evidence_root"], kwargs["publish_root"],
                      kwargs["content_addressed_resume"]))
        return {"skill": skill, "aborted": False, "combinations": {"one": {"lift": 0.1}}}

    monkeypatch.setattr(HC, "run_local_sweep", runner)
    run_catalog(tmp_path, max_skills=2)
    assert [item[0] for item in calls] == ["first", "second"]
    assert all(item[2] is True for item in calls)
    assert _read(first_state)["run_root"].endswith(first.digest)
    assert calls[0][3] == Path(_read(first_state)["run_root"])
    assert calls[0][4] == Path("/srv/ingot/runs/harbor")
    assert calls[0][5] is True
    assert _read(first_state)["status"] == "complete"

    run_catalog(tmp_path, max_skills=2)
    assert len(calls) == 2
    assert _read(tmp_path / "state" / second_path.name)["status"] == "complete"


def test_catalog_forwards_native_process_environment_to_sweep(tmp_path, monkeypatch):
    enqueue_catalog(tmp_path, [_intent()])
    captured = []

    def runner(_skill, _targets, **kwargs):
        captured.append(kwargs["process_env"])
        return {"aborted": False}

    monkeypatch.setattr(HC, "run_local_sweep", runner)
    process_env = {"PATH": "/bin", "HARBOR_EXTRA_DOCKER_COMPOSE": "/tmp/network.yml"}

    run_catalog(tmp_path, max_skills=1, process_env=process_env)

    assert captured == [process_env]


def test_live_controller_refuses_before_runner(tmp_path, monkeypatch):
    enqueue_catalog(tmp_path, [_intent()])
    owner = HC._claim_controller(HC.CATALOG_OWNER)
    monkeypatch.setattr(HC, "run_local_sweep",
                        lambda *_args, **_kwargs: pytest.fail("runner called"))

    try:
        with pytest.raises(RuntimeError, match="live controller"):
            run_catalog(tmp_path)
    finally:
        owner.close()


def test_stale_controller_receipt_is_reused_safely(tmp_path, monkeypatch):
    enqueue_catalog(tmp_path, [_intent()])
    HC.CATALOG_OWNER.write_text(json.dumps({"pid": 123, "start_token": "stale"}))
    monkeypatch.setattr(HC, "run_local_sweep", lambda *_args, **_kwargs: {"aborted": False})

    run_catalog(tmp_path, max_skills=1)
    assert _read(next((tmp_path / "state").glob("*.json")))["status"] == "complete"


def test_failed_skill_is_recorded_and_does_not_abort_sibling(tmp_path, monkeypatch):
    enqueue_catalog(tmp_path, [_intent("bad", priority=200), _intent("good", priority=100)])

    def runner(skill, *_args, **_kwargs):
        if skill == "bad":
            raise RuntimeError("boom")
        return {"aborted": False}

    monkeypatch.setattr(HC, "run_local_sweep", runner)
    run_catalog(tmp_path, max_skills=2)
    states = [_read(path) for path in (tmp_path / "state").glob("*.json")]
    assert {state["status"] for state in states} == {"complete", "failed"}
    assert next(state for state in states if state["status"] == "failed")["error"] == "RuntimeError"

    monkeypatch.setattr(HC, "run_local_sweep", lambda *_args, **_kwargs: {"aborted": False})
    run_catalog(tmp_path, max_skills=1)
    assert {state["status"] for state in
            (_read(path) for path in (tmp_path / "state").glob("*.json"))} == {"complete"}


def test_identity_change_enqueues_current_and_runs_no_model(tmp_path, monkeypatch):
    old = _intent(sha="a" * 64)
    enqueue_catalog(tmp_path, [old])
    current = _intent(sha="c" * 64)
    monkeypatch.setattr(HC, "_intent_for_skill", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(HC, "run_local_sweep",
                        lambda *_args, **_kwargs: pytest.fail("runner called"))

    run_catalog(tmp_path, max_skills=1)

    states = [_read(path) for path in (tmp_path / "state").glob("*.json")]
    assert {state["status"] for state in states} == {"superseded", "pending"}
    assert next(state for state in states if state["status"] == "pending")["priority"] == 300


def test_tampered_intent_or_state_digest_refuses_before_runner(tmp_path, monkeypatch):
    [intent_path] = enqueue_catalog(tmp_path, [_intent()])
    document = _read(intent_path)
    document["identity"]["skill"] = "tampered"
    intent_path.write_text(json.dumps(document))
    monkeypatch.setattr(HC, "run_local_sweep",
                        lambda *_args, **_kwargs: pytest.fail("runner called"))
    with pytest.raises(RuntimeError, match="intent digest"):
        run_catalog(tmp_path)

    intent_path.write_text(json.dumps(HC._intent_document(_intent())))
    state_path = tmp_path / "state" / intent_path.name
    state = _read(state_path)
    state["intent_digest"] = "0" * 64
    state_path.write_text(json.dumps(state))
    with pytest.raises(RuntimeError, match="state digest"):
        run_catalog(tmp_path)


def test_live_harbor_child_refuses_before_runner(tmp_path, monkeypatch):
    intent = _intent()
    enqueue_catalog(tmp_path, [intent])
    monkeypatch.setattr(HC, "_refuse_live_harbor",
                        lambda _root: (_ for _ in ()).throw(RuntimeError("live Harbor child")))
    monkeypatch.setattr(HC, "run_local_sweep",
                        lambda *_args, **_kwargs: pytest.fail("runner called"))

    with pytest.raises(RuntimeError, match="live Harbor child"):
        run_catalog(tmp_path)


def test_low_utilization_is_reported_not_used_to_overlap_skills(tmp_path, monkeypatch):
    enqueue_catalog(tmp_path, [_intent("first"), _intent("second")])
    active = 0
    peak = 0

    def runner(*_args, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        active -= 1
        return {"aborted": False, "utilization": 0.1}

    monkeypatch.setattr(HC, "run_local_sweep", runner)
    run_catalog(tmp_path, max_skills=2)
    assert peak == 1
    assert all("utilization" in _read(path) for path in (tmp_path / "state").glob("*.json"))


def test_telemetry_pending_state_retries_on_next_controller_without_marking_complete(tmp_path,
                                                                                     monkeypatch):
    enqueue_catalog(tmp_path, [_intent()])
    calls = 0

    def runner(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"aborted": False, "telemetry_pending": calls == 1}

    monkeypatch.setattr(HC, "run_local_sweep", runner)
    run_catalog(tmp_path, max_skills=1)
    state_path = next((tmp_path / "state").glob("*.json"))
    assert _read(state_path)["status"] == "failed"
    run_catalog(tmp_path, max_skills=1)
    assert _read(state_path)["status"] == "complete"
    assert calls == 2


def test_all_skips_skills_without_tasks_and_enqueues_eligible_siblings(tmp_path, monkeypatch):
    class Skill:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(HC, "load_skills", lambda: [Skill("missing"), Skill("eligible")])
    monkeypatch.setattr(HC, "_intent_for_skill",
                        lambda skill, *_args, **_kwargs: None if skill == "missing" else _intent(skill))

    assert HC.main(["--root", str(tmp_path), "--all", "--target",
                    "dell-qwen=http://127.0.0.1:8011", "--enqueue-only"]) == 0
    documents = [_read(path) for path in (tmp_path / "intents").glob("*.json")]
    assert [item["identity"]["skill"] for item in documents] == ["eligible"]


def test_intent_route_revision_uses_discovered_target_context(tmp_path, monkeypatch):
    monkeypatch.setattr(HC, "_intent_for_skill", _REAL_INTENT_FOR_SKILL)
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: x\n---\nbody")
    monkeypatch.setattr(HC, "resolve_skill_dir", lambda _name: skill)
    monkeypatch.setattr(HC, "_heldout", lambda _name: [{"task": "x"}] * 4)
    live = HC.parse_target("dell-qwen=http://host:8011")
    live = HC.LocalTarget(**{**live.__dict__, "context_length": 163840})
    monkeypatch.setattr(HC, "discover_target", lambda *_args: live)

    intent = HC._intent_for_skill("demo", ("dell-qwen=http://host:8011",),
                                  ("claude-code",))

    revisions = dict(intent.runtime_revisions)
    assert any(key.startswith(f"route:{live.fingerprint}:claude-code")
               and "20480" in value and "context=163840" in value
               for key, value in revisions.items())


def test_execution_stages_full_tree_atomically_and_rejects_partial_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(HC, "_prepare_execution", _REAL_PREPARE_EXECUTION)
    source = tmp_path / "library" / "demo"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: x\n---\nbody")
    (source / "reference.md").write_text("evidence")
    revision = HC.skill_revision(source)
    intent = _intent()
    intent = CatalogIntent(**{**intent.__dict__,
                              "runtime_revisions": (("skill-tree", revision),)})
    monkeypatch.setattr(HC, "resolve_skill_dir", lambda _skill: source)

    staged, execution = HC._prepare_execution(tmp_path, intent)
    assert (staged / "demo" / "reference.md").read_text() == "evidence"
    assert not list(execution.glob(".staged.*.tmp"))

    (staged / "demo" / "reference.md").unlink()
    with pytest.raises(RuntimeError, match="skill tree identity"):
        HC._prepare_execution(tmp_path, intent)


def test_execution_rejects_source_tree_change_during_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(HC, "_prepare_execution", _REAL_PREPARE_EXECUTION)
    source = tmp_path / "library" / "demo"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: x\n---\nbody")
    (source / "reference.md").write_text("before")
    revision = HC.skill_revision(source)
    intent = CatalogIntent(**{**_intent().__dict__,
                              "runtime_revisions": (("skill-tree", revision),)})
    monkeypatch.setattr(HC, "resolve_skill_dir", lambda _skill: source)
    original = HC.shutil.copytree

    def changed_copy(source_path, destination):
        result = original(source_path, destination)
        (destination / "reference.md").write_text("after")
        return result

    monkeypatch.setattr(HC.shutil, "copytree", changed_copy)
    with pytest.raises(RuntimeError, match="changed while staging"):
        HC._prepare_execution(tmp_path, intent)
    assert not (tmp_path / "runs" / intent.digest / "staged").exists()
