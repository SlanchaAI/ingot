import json
from pathlib import Path

import pytest

from ingot.optimize.harbor_native import (NATIVE_RUNNER_REVISION, NativeCell, NativeTrialIdentity,
                                    compile_agent_config, compile_measurement_job, identity_env,
                                    read_trial_identity)
from ingot.optimize.harbor_targets import LocalTarget


def _target() -> LocalTarget:
    return LocalTarget(
        alias="dell-qwen",
        display_name="Qwen/Qwen3.6-27B",
        base_url="http://127.0.0.1:8011",
        served_model="dot-backbone",
        context_length=163_840,
        protocols=frozenset({"chat", "messages", "responses"}),
        family="Qwen3.6",
        parameter_billions=27.0,
        quantization="fp8-published",
        tool_parser="qwen3_xml",
    )


def test_native_runner_revision_encodes_exact_trial_memory_limit():
    assert NATIVE_RUNNER_REVISION == "native-v4-memory2048mb"


def _identity(arm: str = "skill") -> NativeTrialIdentity:
    return NativeTrialIdentity(
        combination_id="aider@dot-backbone--dell-qwen-deadbeefcafe",
        endpoint_fingerprint="deadbeefcafe",
        harness="aider",
        protocol="chat",
        gateway_revision="direct",
        arm=arm,
    )


def _write_lock(path: Path, env: dict[str, str]) -> None:
    path.mkdir()
    (path / "lock.json").write_text(json.dumps({"agent": {"env": env}}))


def test_trial_identity_round_trips_through_harbor_agent_lock(tmp_path):
    skill = _identity("skill")
    control = _identity("control")
    skill_dir = tmp_path / "skill"
    control_dir = tmp_path / "control"
    _write_lock(skill_dir, {"OPENAI_API_KEY": "${OPENAI_API_KEY}", **identity_env(skill)})
    _write_lock(control_dir, {"OPENAI_API_KEY": "${OPENAI_API_KEY}", **identity_env(control)})

    assert read_trial_identity(skill_dir) == skill
    assert read_trial_identity(control_dir) == control
    assert read_trial_identity(skill_dir) != read_trial_identity(control_dir)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda env: env.pop("INGOT_ARM"),
        lambda env: env.__setitem__("INGOT_ARM", "treatment"),
        lambda env: env.__setitem__("INGOT_HARNESS", "unknown"),
        lambda env: env.__setitem__("INGOT_PROTOCOL", "responses"),
        lambda env: env.__setitem__("INGOT_ENDPOINT_FINGERPRINT", "not-a-fingerprint"),
        lambda env: env.__setitem__("INGOT_EXTRA", "surprise"),
        lambda env: env.__setitem__("INGOT_COMBINATION_ID", "../../mutable/path"),
        lambda env: env.__setitem__("INGOT_ARM", 7),
    ],
)
def test_trial_identity_rejects_malformed_or_ambiguous_lock(tmp_path, mutate):
    env = identity_env(_identity())
    mutate(env)
    trial = tmp_path / "trial"
    _write_lock(trial, env)

    with pytest.raises(ValueError, match="Harbor trial identity"):
        read_trial_identity(trial)


def test_trial_identity_rejects_invalid_lock_shape(tmp_path):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "lock.json").write_text("[]")

    with pytest.raises(ValueError, match="Harbor trial identity"):
        read_trial_identity(trial)


def test_agent_compiler_preserves_arm_and_shared_endpoint_limit(tmp_path):
    target = _target()
    source = tmp_path / "skill-source"
    source.mkdir()
    skill = compile_agent_config(target, "aider", "skill", source, 8)
    control = compile_agent_config(target, "aider", "control", source, 8)

    assert skill["name"] == control["name"] == "aider"
    assert skill["model_name"] == control["model_name"] == "openai/dot-backbone"
    assert skill["n_concurrent"] == control["n_concurrent"] == 8
    assert skill["concurrency_group"] == control["concurrency_group"] == f"endpoint:{target.fingerprint}"
    assert skill["skills"] == [str(source)]
    assert control["skills"] == []
    assert skill["env"]["INGOT_ARM"] == "skill"
    assert control["env"]["INGOT_ARM"] == "control"
    assert skill["env"]["INGOT_COMBINATION_ID"] == (
        f"aider@{target.served_model}--{target.job_slug}")
    assert skill["env"]["INGOT_GATEWAY_REVISION"] == f"direct-{NATIVE_RUNNER_REVISION}"
    assert skill["env"]["OPENAI_API_KEY"] == control["env"]["OPENAI_API_KEY"] == "local"


def test_measurement_compiler_can_resume_only_the_missing_arm(tmp_path):
    target = _target()
    cell = NativeCell(target, "aider")

    config = compile_measurement_job(
        tmp_path / "dataset", [f"task-{index}" for index in range(4)], [cell],
        tmp_path / "skill", tmp_path / "jobs", attempts=3, global_limit=4,
        endpoint_limits={target.fingerprint: 4}, arms=("control",))

    assert [agent["env"]["INGOT_ARM"] for agent in config["agents"]] == ["control"]


def test_agent_compiler_uses_import_path_for_gateway_codex(tmp_path):
    target = _target()
    from ingot.optimize.harbor_gateway import gateway_route

    route = gateway_route(target, "codex")
    assert route is not None
    config = compile_agent_config(target, "codex", "skill", tmp_path, 4)

    assert config["import_path"] == "ingot.optimize.harbor_codex_gateway:GatewayCodex"
    assert "name" not in config
    assert config["concurrency_group"] == f"endpoint:{target.fingerprint}"
    assert config["env"]["INGOT_GATEWAY_REVISION"] == f"{route.revision}-{NATIVE_RUNNER_REVISION}"


def test_iter_attempt_dirs_filters_unified_job_by_exact_identity(tmp_path):
    from ingot.optimize.harbor_native import iter_attempt_dirs

    wanted = _identity("skill")
    other = _identity("control")
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_lock(first, identity_env(wanted))
    _write_lock(second, identity_env(other))
    (first / "result.json").write_text('{"task_name":"ingot/h1"}')
    (second / "result.json").write_text('{"task_name":"ingot/h1"}')

    assert list(iter_attempt_dirs(tmp_path, identity=wanted)) == [first]
    assert list(iter_attempt_dirs(tmp_path, identity=other)) == [second]


def test_iter_attempt_dirs_does_not_inspect_unselected_malformed_sibling(tmp_path):
    from ingot.optimize.harbor_native import iter_attempt_dirs

    wanted = _identity("skill")
    first = tmp_path / "first"
    malformed = tmp_path / "malformed"
    _write_lock(first, identity_env(wanted))
    malformed.mkdir()
    (malformed / "lock.json").write_text("not-json")
    (first / "result.json").write_text('{"task_name":"ingot/h1"}')
    (malformed / "result.json").write_text('{"task_name":"ingot/h1"}')

    assert list(iter_attempt_dirs(tmp_path, identity=wanted)) == [first]


def test_iter_attempt_dirs_rejects_symlinked_attempt(tmp_path):
    from ingot.optimize.harbor_native import iter_attempt_dirs

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.json").write_text('{"task_name":"ingot/h1"}')
    job = tmp_path / "job"
    job.mkdir()
    (job / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="real attempt directory"):
        list(iter_attempt_dirs(job))


@pytest.mark.parametrize("name", ["lock.json", "result.json"])
def test_native_attempt_rejects_symlinked_identity_or_result_file(tmp_path, name):
    from ingot.optimize.harbor_native import iter_attempt_dirs

    job = tmp_path / "job"
    trial = job / "h1__one"
    trial.mkdir(parents=True)
    external = tmp_path / f"external-{name}"
    external.write_text('{}')
    (trial / "lock.json").write_text(json.dumps({"agent": {"env": identity_env(_identity())}}))
    (trial / "result.json").write_text('{"task_name":"ingot/h1"}')
    (trial / name).unlink()
    (trial / name).symlink_to(external)

    with pytest.raises(ValueError, match="regular file"):
        list(iter_attempt_dirs(job, identity=_identity()))
