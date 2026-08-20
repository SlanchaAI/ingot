import pytest

from ingot.optimize.harbor_native import (NativeCell, compile_canary_job,
                                    compile_measurement_job,
                                    select_measurement_cells, write_job_config)
from ingot.optimize.harbor_targets import LocalTarget


def _target(alias: str, model: str, port: int) -> LocalTarget:
    return LocalTarget(
        alias=alias,
        display_name=model,
        base_url=f"http://127.0.0.1:{port}",
        served_model=model,
        context_length=131_072,
        protocols=frozenset({"chat", "messages", "responses"}),
    )


@pytest.fixture
def cells():
    first = _target("dell-qwen", "dot-backbone", 8011)
    second = _target("spark-deepseek", "deepseek-v4-flash", 8000)
    return [NativeCell(first, "aider"), NativeCell(second, "goose")]


def test_canary_job_compiles_one_skill_agent_per_cell(tmp_path, cells):
    config = compile_canary_job(
        tmp_path / "dataset", "build-loop-h1", cells, tmp_path / "skill", tmp_path / "jobs",
        global_limit=16, endpoint_limits={cell.target.fingerprint: 4 for cell in cells},
    )

    assert config["jobs_dir"] == str(tmp_path / "jobs")
    assert config["n_attempts"] == 1
    assert config["n_concurrent_trials"] == 16
    assert config["datasets"] == [{"path": str(tmp_path / "dataset"),
                                    "task_names": ["build-loop-h1"]}]
    assert len(config["agents"]) == 2
    assert [agent["env"]["INGOT_ARM"] for agent in config["agents"]] == ["canary", "canary"]
    assert all(agent["skills"] == [str(tmp_path / "skill")] for agent in config["agents"])


def test_measurement_job_interleaves_matched_arms_and_shares_endpoint_caps(tmp_path, cells):
    limits = {cells[0].target.fingerprint: 8, cells[1].target.fingerprint: 12}
    config = compile_measurement_job(
        tmp_path / "dataset", ["h1", "h2", "h3", "h4"], cells,
        tmp_path / "skill", tmp_path / "jobs", attempts=3, global_limit=32,
        endpoint_limits=limits,
    )

    assert config["n_attempts"] == 3
    assert config["n_concurrent_trials"] == 32
    assert config["datasets"] == [{"path": str(tmp_path / "dataset"),
                                    "task_names": ["h1", "h2", "h3", "h4"]}]
    assert [agent["env"]["INGOT_ARM"] for agent in config["agents"]] == [
        "skill", "control", "skill", "control"]
    assert [agent["env"]["INGOT_COMBINATION_ID"] for agent in config["agents"]][::2] == [
        cells[0].combination_id, cells[1].combination_id]
    for index, cell in enumerate(cells):
        pair = config["agents"][index * 2:index * 2 + 2]
        assert {agent["n_concurrent"] for agent in pair} == {limits[cell.target.fingerprint]}
        assert {agent["concurrency_group"] for agent in pair} == {
            f"endpoint:{cell.target.fingerprint}"}


def test_measurement_job_round_robins_target_major_input(tmp_path):
    first = _target("dell-qwen", "dot-backbone", 8011)
    second = _target("spark-deepseek", "deepseek-v4-flash", 8000)
    cells = [NativeCell(first, "aider"), NativeCell(first, "goose"),
             NativeCell(second, "aider"), NativeCell(second, "goose")]
    config = compile_measurement_job(
        tmp_path / "dataset", ["h1", "h2", "h3", "h4"], cells,
        tmp_path / "skill", tmp_path / "jobs", attempts=3, global_limit=32,
        endpoint_limits={first.fingerprint: 8, second.fingerprint: 8},
    )

    skill_agents = config["agents"][::2]
    assert [agent["env"]["INGOT_ENDPOINT_FINGERPRINT"] for agent in skill_agents] == [
        first.fingerprint, second.fingerprint, first.fingerprint, second.fingerprint]


@pytest.mark.parametrize("attempts", [0, 1, 2, 4, True])
def test_measurement_job_requires_full_three_attempt_contract(tmp_path, cells, attempts):
    with pytest.raises(ValueError, match="three attempts"):
        compile_measurement_job(
            tmp_path / "dataset", ["h1", "h2", "h3", "h4"], cells,
            tmp_path / "skill", tmp_path / "jobs", attempts=attempts, global_limit=16,
            endpoint_limits={cell.target.fingerprint: 4 for cell in cells},
        )


def test_job_compiler_refuses_missing_or_unknown_endpoint_limit(tmp_path, cells):
    with pytest.raises(ValueError, match="endpoint concurrency"):
        compile_canary_job(
            tmp_path / "dataset", "h1", cells, tmp_path / "skill", tmp_path / "jobs",
            global_limit=16, endpoint_limits={cells[0].target.fingerprint: 4},
        )


def test_failed_canary_becomes_explicit_unmeasured_cell_without_lift(cells):
    selected, unmeasured = select_measurement_cells(cells, {
        cells[0].combination_id: {"ok": True},
        cells[1].combination_id: {"error": "AgentTimeoutError: expired"},
    })

    assert selected == [cells[0]]
    assert unmeasured == {cells[1].combination_id: {
        "combination": cells[1].combination_id,
        "harness": cells[1].harness,
        "target_alias": cells[1].target.alias,
        "endpoint_fingerprint": cells[1].target.fingerprint,
        "state": "unmeasured",
        "error": "AgentTimeoutError: expired",
    }}
    assert "lift" not in unmeasured[cells[1].combination_id]


def test_job_config_write_is_atomic_and_deterministic(tmp_path, cells):
    config = compile_canary_job(
        tmp_path / "dataset", "h1", cells, tmp_path / "skill", tmp_path / "jobs",
        global_limit=16, endpoint_limits={cell.target.fingerprint: 4 for cell in cells},
    )
    output = tmp_path / "config.json"

    write_job_config(output, config)
    first = output.read_bytes()
    write_job_config(output, config)

    assert output.read_bytes() == first
    assert not list(tmp_path.glob(".config.json.*.tmp"))


@pytest.mark.parametrize("name", ["http:config.json", "model@host:8011.json"])
def test_job_config_refuses_url_or_port_derived_filename(tmp_path, cells, name):
    config = compile_canary_job(
        tmp_path / "dataset", "h1", cells, tmp_path / "skill", tmp_path / "jobs",
        global_limit=16, endpoint_limits={cell.target.fingerprint: 4 for cell in cells},
    )

    with pytest.raises(ValueError, match="safe slug"):
        write_job_config(tmp_path / name, config)
