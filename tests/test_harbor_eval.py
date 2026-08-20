"""Unit tests for the sandboxed cross-harness skill eval (no Docker, no network, no harness).

The Harbor subprocess and the judge are stubbed; what is exercised here is the part that is ours:
the generated dataset, the treatment/control arms, the trial-name parsing that reads Harbor's
output layout, and the scoring of what a harness actually produced."""
import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from ingot.optimize import agy_judge as A
from ingot.optimize import harbor_eval as H
from ingot.optimize.harbor_targets import LocalTarget


HOLDOUT = [{"task": "Write add(a, b).", "rubric": "defines add"},
           {"task": "Write sub(a, b).", "rubric": "defines sub"}]


@pytest.fixture(autouse=True)
def _avoid_starting_a_gateway_process_in_harbor_unit_tests(monkeypatch):
    """Gateway lifecycle has its own unit tests; these tests only assert orchestration order."""
    class Session:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(H, "GatewaySession", Session)


def test_dataset_has_one_task_per_holdout_task(tmp_path):
    dataset = H.build_dataset("demo", HOLDOUT, tmp_path)
    names = sorted(p.name for p in dataset.iterdir() if p.is_dir())
    assert names == ["demo-h0", "demo-h1"]
    for index, name in enumerate(names):
        root = dataset / name
        assert (root / "environment" / "Dockerfile").is_file()
        assert (root / "tests" / "test.sh").is_file()
        instruction = (root / "instruction.md").read_text()
        assert HOLDOUT[index]["task"] in instruction
        # Without this the agent answers in chat and the workspace stays empty, which the collector
        # would then score as a zero for every task.
        assert H.SOLUTION_DIR in instruction
    assert 'name = "ingot/demo"' in (dataset / "dataset.toml").read_text()


SEEDED = [{"task": "Fix the backup.", "rubric": "fixes it",
           "files": {"backup.sh": "#!/bin/bash\necho hi\n", "tools/check.py": "print(1)\n"},
           "verify": 'cd /tmp && python3 -c "print(\'ok\')"'}]


def test_a_seeded_task_ships_its_working_tree_into_the_image(tmp_path):
    """A process skill has nothing to act on in an empty container. Verifying in the execution
    context, feeding a guard its reject input and wiring a real caller all need existing code."""
    root = H.build_dataset("demo", SEEDED, tmp_path) / "demo-h0"
    seed = root / "environment" / "seed"
    assert (seed / "backup.sh").read_text() == "#!/bin/bash\necho hi\n"
    assert (seed / "tools" / "check.py").read_text() == "print(1)\n"
    dockerfile = (root / "environment" / "Dockerfile").read_text()
    assert f"COPY seed/ {H.REPO_DIR}/" in dockerfile
    # The agent is told where the tree is; without this it starts from a blank directory and the
    # seeded defect is never seen.
    assert H.REPO_DIR in (root / "instruction.md").read_text()


def test_an_unseeded_task_keeps_the_blank_environment(tmp_path):
    """Seeding is opt-in per task: a task with no files must build the image it always built."""
    root = H.build_dataset("demo", HOLDOUT, tmp_path) / "demo-h0"
    assert not (root / "environment" / "seed").exists()
    assert "COPY seed/" not in (root / "environment" / "Dockerfile").read_text()
    assert "_objective_check" not in (root / "tests" / "test.sh").read_text()


def test_the_objective_check_runs_after_the_agent_and_survives_quoting(tmp_path):
    """The agent's own evidence log is a claim about what it ran, and a skill that rewards writing
    evidence logs is exactly what teaches it to produce one. This is the referent from outside."""
    root = H.build_dataset("demo", SEEDED, tmp_path) / "demo-h0"
    test_sh = (root / "tests" / "test.sh").read_text()
    assert "_objective_check.txt" in test_sh
    # The command embeds both quote kinds. Interpolating it raw produced unrunnable shell.
    assert subprocess.run(["bash", "-n", str(root / "tests" / "test.sh")],
                          capture_output=True).returncode == 0
    # It must stay captured evidence, never a second grader: the Ingot judge owns the score.
    assert "echo 1 > /logs/verifier/reward.txt" in test_sh


def test_a_seeded_path_cannot_escape_the_task_directory(tmp_path):
    """Task files are authored data, but a traversing key would write into this checkout rather
    than the container's."""
    escaping = [{"task": "t", "rubric": "r", "files": {"../../pwned": "x"}}]
    with pytest.raises(ValueError, match="escapes"):
        H.build_dataset("demo", escaping, tmp_path)
    assert not (tmp_path.parent / "pwned").exists()


def test_rebuilding_a_dataset_drops_tasks_from_a_shorter_holdout(tmp_path):
    """A holdout that shrinks must not leave last run's extra task behind to be run and scored."""
    H.build_dataset("demo", HOLDOUT, tmp_path)
    dataset = H.build_dataset("demo", HOLDOUT[:1], tmp_path)
    assert sorted(p.name for p in dataset.iterdir() if p.is_dir()) == ["demo-h0"]


def test_the_skill_arm_passes_a_skill_and_the_control_arm_does_not(tmp_path, monkeypatch):
    """Lift is the difference between these two commands. If the control also carried --skill there
    would be no control at all, and every lift would come out at zero."""
    seen = []

    class Done:
        returncode, stderr, stdout = 0, "", ""

    monkeypatch.setattr(H.subprocess, "run", lambda argv, **kw: seen.append(argv) or Done())
    monkeypatch.setattr(H, "_refuse_broken_job", lambda *a: None)   # covered by its own tests
    H.run_arm(tmp_path / "ds", "claude-code", "/skills/demo", tmp_path / "jobs", "skill",
              log=lambda *a: None)
    H.run_arm(tmp_path / "ds", "claude-code", None, tmp_path / "jobs", "control",
              log=lambda *a: None)

    assert "--skill" in seen[0] and seen[0][seen[0].index("--skill") + 1] == "/skills/demo"
    assert "--skill" not in seen[1]
    assert "--path" in seen[0], "a local dataset must be passed with --path, not --dataset"


def test_run_arm_forwards_agent_env_agent_kwargs_task_name_without_provider_leak(
    tmp_path, monkeypatch
):
    """Local adapter settings cross the Harbor boundary without exposing the parent credentials."""
    seen = []
    logs = []

    class Done:
        returncode, stderr, stdout = 0, "", ""

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return Done()

    monkeypatch.setattr(H.subprocess, "run", fake_run)
    monkeypatch.setattr(H, "_refuse_broken_job", lambda *a: None)
    secret = "sk-parent-secret"
    child_env = {"PATH": "/usr/bin", "OPENAI_API_KEY": secret,
                 "ANTHROPIC_API_KEY": "sk-anthropic-parent", "SAFE": "yes",
                 "SAFE_SECRET": secret,
                 "LANGFUSE_PUBLIC_KEY": "pk-parent",
                 "LANGFUSE_SECRET_KEY": "sk-parent",
                 "LANGFUSE_BASE_URL": "https://langfuse-parent.invalid",
                 "LANGFUSE_ENCRYPTION_KEY": "encryption-parent",
                 "LANGFUSE_INIT_USER_PASSWORD": "password-parent",
                 "LANGFUSE_PUBLIC_URL": "https://public-parent.invalid"}
    H.run_arm(
        tmp_path / "ds", "terminus-2", None, tmp_path / "jobs", "local",
        model="deepseek-v4-flash", concurrency=3, attempts=2,
        agent_env={"Z_KEY": "z-value", "A_KEY": "a-value", "OPENAI_API_KEY": "local",
                   "LANGFUSE_PUBLIC_KEY": "pk-agent",
                   "LANGFUSE_SECRET_KEY": "sk-agent",
                   "LANGFUSE_BASE_URL": "https://langfuse-agent.invalid",
                   "LANGFUSE_ENCRYPTION_KEY": "encryption-agent",
                   "LANGFUSE_INIT_USER_PASSWORD": "password-agent",
                   "LANGFUSE_PUBLIC_URL": "https://public-agent.invalid"},
        agent_kwargs={"zeta": "z-value", "alpha": "a-value"},
        task_name="demo-h0", process_env=child_env, log=logs.append,
    )

    argv, kwargs = seen[0]
    assert [argv[i + 1] for i, value in enumerate(argv) if value == "--ae"] == [
        "A_KEY=a-value", "OPENAI_API_KEY=local", "Z_KEY=z-value"
    ]
    assert [argv[i + 1] for i, value in enumerate(argv) if value == "--ak"] == [
        "alpha=a-value", "zeta=z-value"
    ]
    assert argv[argv.index("--include-task-name") + 1] == "demo-h0"
    assert kwargs["env"] is not child_env
    assert kwargs["env"]["PATH"] == "/usr/bin" and kwargs["env"]["SAFE"] == "yes"
    assert kwargs["env"]["OPENAI_API_KEY"] == "local"
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    assert not any(key.startswith("LANGFUSE_") for key in kwargs["env"])
    assert not any("LANGFUSE_" in value for value in argv)
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert "OPENAI_API_KEY=local" in argv
    assert secret not in " ".join(argv)
    assert secret not in "\n".join(logs)
    assert secret not in str(tmp_path / "jobs" / "local")


def test_run_arm_serializes_nested_agent_kwargs_as_json(tmp_path, monkeypatch):
    seen = []

    class Done:
        returncode, stderr, stdout = 0, "", ""

    monkeypatch.setattr(H.subprocess, "run", lambda argv, **kwargs: seen.append(argv) or Done())
    monkeypatch.setattr(H, "_refuse_broken_job", lambda *a: None)
    H.run_arm(tmp_path / "ds", "opencode", None, tmp_path / "jobs", "local",
              agent_kwargs={"opencode_config": {"provider": {"local": {"npm": "x"}}}})
    value = seen[0][seen[0].index("--ak") + 1]
    assert value == 'opencode_config={"provider":{"local":{"npm":"x"}}}'


def test_run_arm_without_local_overrides_keeps_legacy_command_and_process_call(
    tmp_path, monkeypatch
):
    """Existing proprietary callers keep the old command and inherited process environment."""
    seen = []

    class Done:
        returncode, stderr, stdout = 0, "", ""

    monkeypatch.setenv("LANGFUSE_ENCRYPTION_KEY", "parent-encryption")
    monkeypatch.setenv("LANGFUSE_PUBLIC_URL", "https://public.invalid")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-key-must-remain")
    monkeypatch.setattr(H.subprocess, "run", lambda argv, **kwargs: seen.append((argv, kwargs)) or Done())
    monkeypatch.setattr(H, "_refuse_broken_job", lambda *a: None)
    H.run_arm(tmp_path / "ds", "claude-code", None, tmp_path / "jobs", "control",
              log=lambda *a: None)
    argv, kwargs = seen[0]
    assert argv == [
        H.HARBOR_BIN, "run", "--path", str(tmp_path / "ds"), "--agent", "claude-code",
        "--n-concurrent", "2", "--jobs-dir", str(tmp_path / "jobs"), "--job-name", "control",
        "--environment-build-timeout-multiplier", str(H.BUILD_TIMEOUT_MULTIPLIER),
        "--agent-setup-timeout-multiplier", str(H.SETUP_TIMEOUT_MULTIPLIER),
    ]
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert not any(key.startswith("LANGFUSE_") for key in kwargs["env"])
    assert kwargs["env"]["OPENAI_API_KEY"] == "provider-key-must-remain"


def test_a_failing_harbor_run_is_raised_not_silently_scored(tmp_path, monkeypatch):
    class Done:
        returncode, stderr, stdout = 1, "boom", "outer stdout"

    monkeypatch.setattr(H.subprocess, "run", lambda argv, **kw: Done())
    with pytest.raises(RuntimeError, match="boom"):
        H.run_arm(tmp_path / "ds", "codex", None, tmp_path / "jobs", "control", log=lambda *a: None)
    assert json.loads((tmp_path / "jobs" / "control" / "harbor-invocation.json").read_text()) == {
        "returncode": 1, "stdout_bytes": 12, "stderr_bytes": 4,
        "stdout_excerpt": "outer stdout", "stderr_excerpt": "boom",
    }


def test_run_arm_keeps_outer_harbor_exit_evidence_when_a_pending_job_is_refused(tmp_path, monkeypatch):
    """A Harbor zero exit can still leave a pending job; preserve the only outer diagnostic."""
    class Done:
        returncode = 0
        stdout = "Authorization: Bearer bearer-value http://target.invalid:8001/sk-path"
        stderr = ('x-api-key: header-value Authorization: Basic basic-value '
                  'sk-live-value pk_live_value known-secret-value '
                  '"OPENAI_API_KEY": "json-value" LITELLM_API_KEY: colon-value '
                  'MODEL_API_KEY space-value internal.example:8001')

    parent = {"OPENAI_API_KEY": "known-secret-value", "CUSTOM_API_KEY": "mapping-secret"}
    monkeypatch.setattr(H.subprocess, "run", lambda argv, **kw: Done())

    def refuse_pending(*args):
        raise RuntimeError("canary wrote no completed trial")

    monkeypatch.setattr(H, "_refuse_broken_job", refuse_pending)
    job = tmp_path / "jobs" / "codex"
    with pytest.raises(RuntimeError, match="canary wrote no completed trial"):
        H.run_arm(tmp_path / "ds", "codex", None, tmp_path / "jobs", "codex",
                  process_env=parent, log=lambda *a: None)
    receipt_path = job / "harbor-invocation.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["returncode"] == 0
    assert receipt["stdout_bytes"] == len(Done.stdout.encode())
    assert receipt["stderr_bytes"] == len(Done.stderr.encode())
    persisted = receipt_path.read_text()
    for raw in ("target.invalid", "bearer-value", "header-value", "basic-value", "sk-live-value",
                "pk_live_value", "known-secret-value", "mapping-secret", "json-value",
                "colon-value", "space-value", "internal.example:8001"):
        assert raw not in persisted
    assert receipt["stdout_excerpt"] and receipt["stderr_excerpt"]


def test_trial_name_drops_the_run_suffix_harbor_appends(tmp_path):
    """Observed layout: jobs/<job>/<task>__<suffix>/verifier/solution. Keeping the suffix means no
    trial ever matches its task and every score reads as a zero."""
    solution = tmp_path / "demo-h1__abc123" / "verifier" / "solution"
    solution.mkdir(parents=True)
    assert H._trial_task_name(solution) == "demo-h1"


def test_collect_reads_what_the_agent_left_in_the_solution_directory(tmp_path):
    solution = tmp_path / "demo-h0__xy" / "verifier" / "solution"
    (solution / "pkg").mkdir(parents=True)
    (solution / "answer.py").write_text("def add(a, b): return a + b")
    (solution / "pkg" / "notes.md").write_text("reasoning")
    answers = H.collect_answers(tmp_path)
    assert set(answers) == {"demo-h0"}
    assert "def add" in answers["demo-h0"][0] and "reasoning" in answers["demo-h0"][0]


def test_repeated_attempts_at_one_task_are_all_kept(tmp_path):
    """Keying a single answer by task name silently kept only whichever trial was read last, so
    --n-attempts above 1 paid for repeated measurements and then threw all but one away. Repetition
    is the whole remedy for this eval's noise: re-judging a fixed answer three times returned an
    identical score, while re-running the agent on the same task moved it by 0.278."""
    for suffix, body in (("aa", "first attempt"), ("bb", "second attempt")):
        solution = tmp_path / f"demo-h0__{suffix}" / "verifier" / "solution"
        solution.mkdir(parents=True)
        (solution / "answer.py").write_text(body)
    answers = H.collect_answers(tmp_path)
    assert len(answers["demo-h0"]) == 2
    assert {"first attempt", "second attempt"} == {a.split("\n", 1)[1] for a in answers["demo-h0"]}


def test_a_tasks_score_is_the_mean_over_its_attempts(monkeypatch):
    scores = iter([1.0, 0.0])
    monkeypatch.setattr(H, "judge", lambda *a, **k: {"score": next(scores)})
    got = H.score({"demo-h0": ["one", "two"]}, "demo", HOLDOUT[:1])
    assert got == [0.5]


def test_arm_scoring_runs_independent_grades_with_bounded_concurrency(monkeypatch):
    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_judge(*_args, **_kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return {"score": 0.5}

    monkeypatch.setattr(H, "judge", fake_judge)
    answers = {f"demo-h{index}": ["one", "two", "three"] for index in range(2)}

    scores = H.score(answers, "demo", HOLDOUT, concurrency=4)

    assert scores == [0.5, 0.5]
    assert 1 < peak <= 4


def test_a_harness_that_produced_nothing_scores_zero_rather_than_being_dropped(monkeypatch):
    """Dropping the empty task would raise the arm's mean by removing its own failure. A harness
    that ran and delivered nothing has a real score, and it is zero."""
    monkeypatch.setattr(H, "judge", lambda *a, **k: {"score": 0.8})
    scores = H.score({"demo-h0": ["some code"]}, "demo", HOLDOUT)
    assert scores == [0.8, 0.0]


def test_the_tasks_own_checklist_reaches_the_judge(monkeypatch):
    """A task's checklist is what gives its score any resolution.

    `judge()` grades on four generic items unless a task supplies its own, and a frontier model
    passes all four on any easy task. Dropping the checklist here is what flattened the first
    build-loop matrix: every control landed near 0.85 and no arm could separate from any other."""
    checklist = [{"id": "declares_stakes_tier", "criterion": "names a tier", "weight": 3,
                  "dimension": "instruction_following"}]
    holdout = [{"task": "Write add(a, b).", "rubric": "defines add", "checklist": checklist}]
    seen = {}

    def fake_judge(task, rubric, answer, **kwargs):
        seen.update(kwargs)
        return {"score": 1.0}

    monkeypatch.setattr(H, "judge", fake_judge)
    H.score({"demo-h0": ["def add(a, b): return a + b"]}, "demo", holdout)
    assert seen["checklist"] == checklist


def test_agy_failure_propagates_out_of_arm_scoring(monkeypatch):
    monkeypatch.setenv("JUDGE_BACKEND", "agy")
    monkeypatch.setattr(
        A,
        "invoke",
        lambda *_args: (_ for _ in ()).throw(A.AgyJudgeError("agy stopped")),
    )

    with pytest.raises(A.AgyJudgeError, match="agy stopped"):
        H.score({"demo-h0": ["answer"]}, "demo", HOLDOUT[:1])


def test_matrix_records_a_broken_harness_without_discarding_the_others(tmp_path, monkeypatch):
    """One missing harness must not throw away the arms already paid for, and its row must carry no
    lift: a blank zero would read as 'measured, no effect'."""
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H, "stage_skill", lambda skill: tmp_path / "source")
    monkeypatch.setattr(H, "stage_skill", lambda skill: tmp_path / "staged")
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setattr(H, "build_dataset", lambda *a, **k: tmp_path / "ds")
    monkeypatch.setattr(H, "HARBOR_DIR", tmp_path / "out")
    monkeypatch.setattr(H, "collect_answers", lambda job, skip=None: {})

    def fake_run_arm(dataset, agent, source, jobs_dir, job_name, *a, **k):
        if agent == "broken":
            raise RuntimeError("no such agent")
        return jobs_dir / job_name

    monkeypatch.setattr(H, "run_arm", fake_run_arm)
    monkeypatch.setattr(H, "broken_tasks", lambda job: set())
    monkeypatch.setattr(H, "score", lambda answers, skill, holdout, skip=None: (
        [1.0, 1.0] if "skill" in str(answers) else [0.5, 0.5]))

    out = H.run_harbor_eval("demo", ["claude-code", "broken"], log=lambda *a: None)
    assert "lift" in out["harnesses"]["claude-code"]
    assert "no such agent" in out["harnesses"]["broken"]["error"]
    assert "lift" not in out["harnesses"]["broken"]
    assert json.loads((tmp_path / "out" / "demo.json").read_text())["skill"] == "demo"


def test_a_run_where_no_harness_worked_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H, "stage_skill", lambda skill: tmp_path / "staged")
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setattr(H, "build_dataset", lambda *a, **k: tmp_path / "ds")
    monkeypatch.setattr(H, "HARBOR_DIR", tmp_path / "out")
    monkeypatch.setattr(H, "run_arm", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(SystemExit, match="nothing was measured"):
        H.run_harbor_eval("demo", ["a", "b"], log=lambda *a: None)
    assert not (tmp_path / "out" / "demo.json").exists()


def test_build_leavings_are_not_fed_to_the_judge(tmp_path):
    """The first real container run left __pycache__/*.pyc beside solution.py. Those bytes went
    into the text the judge grades — noise it pays for and can be misled by."""
    solution = tmp_path / "demo-h0__xy" / "verifier" / "solution"
    (solution / "__pycache__").mkdir(parents=True)
    (solution / "solution.py").write_text("def add(a, b): return a + b")
    (solution / "__pycache__" / "solution.cpython-312.pyc").write_bytes(b"\x00\x01\xfe\xff")
    answers = H.collect_answers(tmp_path)
    assert "def add" in answers["demo-h0"][0]
    assert "pycache" not in answers["demo-h0"][0]


def _job_with_stats(tmp_path, **stats):
    job = tmp_path / "jobs" / "arm"
    job.mkdir(parents=True)
    (job / "result.json").write_text(json.dumps({"stats": stats}))
    return job


def test_an_arm_whose_trials_all_crashed_is_refused_not_scored_as_zeros(tmp_path, monkeypatch):
    """Observed live: harbor exited 0 with n_errored_trials=4, the solution dirs were empty, and
    score() read four legitimate 0.0s — producing lift +0.750 against a working skill arm. An arm
    that did not run is missing, not bad."""
    class Done:
        returncode, stderr, stdout = 0, "", ""

    monkeypatch.setattr(H.subprocess, "run", lambda argv, **kw: Done())
    monkeypatch.setattr(H, "_refuse_broken_job", H._refuse_broken_job)
    job = _job_with_stats(tmp_path, n_completed_trials=4, n_errored_trials=4, n_cancelled_trials=4)
    with pytest.raises(RuntimeError, match="refusing to score"):
        H._refuse_broken_job(job, "terminus-2", "control")


def test_a_clean_arm_is_accepted(tmp_path):
    job = _job_with_stats(tmp_path, n_completed_trials=4, n_errored_trials=0, n_cancelled_trials=0)
    H._refuse_broken_job(job, "terminus-2", "skill")


def test_an_arm_with_no_result_file_is_refused(tmp_path):
    """No result.json means harbor never got far enough to report; scoring it would invent data."""
    with pytest.raises(RuntimeError, match="no result.json"):
        H._refuse_broken_job(tmp_path / "missing", "codex", "skill")


def test_an_arm_that_delivered_nothing_at_all_is_refused_not_scored_as_zeros(monkeypatch):
    """A trial can complete while its agent never ran: the verifier always reports success, so an
    agent that died on its first API call still counts completed with an empty workspace. Observed
    live with aider (temperature rejected by claude-sonnet-5) — it would have scored a clean 0.000
    and read as 'aider is terrible at this skill' rather than 'aider never ran'."""
    monkeypatch.setattr(H, "judge", lambda *a, **k: {"score": 0.9})
    with pytest.raises(RuntimeError, match="empty workspace"):
        H.score({}, "demo", HOLDOUT)


def test_a_partly_empty_arm_still_scores_its_failures_as_zero(monkeypatch):
    """One task delivering nothing is a real failure of that task, not a broken combination."""
    monkeypatch.setattr(H, "judge", lambda *a, **k: {"score": 0.9})
    assert H.score({"demo-h0": "code"}, "demo", HOLDOUT) == [0.9, 0.0]


def _trial(job, task, exception_type=None):
    """Harbor's real trial result shape, not an invented one.

    The first version of this helper wrote a top-level `exception_type`, which Harbor never emits —
    so the guard read nothing, passed its tests, and was a no-op against real output."""
    d = job / f"{task}__xy"
    d.mkdir(parents=True, exist_ok=True)
    record = {"task_name": f"ingot/{task}", "exception_info": None}
    if exception_type:
        record["exception_info"] = {"exception_type": exception_type, "exception_message": ""}
    (d / "result.json").write_text(json.dumps(record))
    return d


def test_broken_tasks_names_only_the_trials_that_failed(tmp_path):
    job = tmp_path / "arm"
    _trial(job, "demo-h0")
    _trial(job, "demo-h1", exception_type="CancelledError")
    assert H.broken_tasks(job) == {"demo-h1"}


def test_one_transient_trial_failure_does_not_discard_the_whole_arm(tmp_path):
    """Failing the arm on any broken trial threw away three good trials and the paid-for opposite
    arm. Observed live: the first grid row died on 1 errored trial of 4."""
    job = tmp_path / "arm"
    job.mkdir()
    (job / "result.json").write_text(json.dumps(
        {"stats": {"n_completed_trials": 4, "n_errored_trials": 1, "n_cancelled_trials": 0}}))
    H._refuse_broken_job(job, "terminus-2", "skill")   # must not raise


def test_an_arm_is_still_refused_when_every_trial_broke(tmp_path):
    job = tmp_path / "arm"
    job.mkdir()
    (job / "result.json").write_text(json.dumps(
        {"stats": {"n_completed_trials": 4, "n_errored_trials": 4, "n_cancelled_trials": 4}}))
    with pytest.raises(RuntimeError, match="every one of its"):
        H._refuse_broken_job(job, "terminus-2", "control")


def test_a_task_dropped_from_one_arm_is_dropped_from_both(monkeypatch):
    """Scoring the arms over different task sets means their difference is not lift."""
    monkeypatch.setattr(H, "judge", lambda *a, **k: {"score": 0.6})
    scores = H.score({"demo-h0": "x", "demo-h1": "y"}, "demo", HOLDOUT, skip={"demo-h1"})
    assert scores == [0.6], "the dropped task must not appear in the scored list"


def test_scoring_refuses_when_every_task_was_dropped(monkeypatch):
    monkeypatch.setattr(H, "judge", lambda *a, **k: {"score": 0.6})
    with pytest.raises(RuntimeError, match="nothing comparable"):
        H.score({"demo-h0": "x"}, "demo", HOLDOUT, skip={"demo-h0", "demo-h1"})


def _attempt(job, task, suffix, *, errored=False, answer="ok"):
    """One trial directory as Harbor lays it out, with or without a recorded exception."""
    trial = job / f"{task}__{suffix}"
    (trial / "verifier" / "solution").mkdir(parents=True)
    (trial / "verifier" / "solution" / "answer.py").write_text(answer)
    record = {"task_name": f"ingot/{task}"}
    if errored:
        record["exception_info"] = {"exception_type": "AgentSetupTimeoutError"}
    (trial / "result.json").write_text(json.dumps(record))
    return trial


def test_one_crashed_attempt_does_not_discard_the_task(tmp_path):
    """With repeats, dropping a task because one attempt broke throws away the attempts that did
    run — which are the entire reason for paying for repeats."""
    job = tmp_path / "control"
    job.mkdir()
    _attempt(job, "demo-h0", "aa", errored=True)
    _attempt(job, "demo-h0", "bb")
    _attempt(job, "demo-h0", "cc")
    assert H.broken_tasks(job) == set()
    assert H.broken_trials(job) == {"demo-h0__aa"}


def test_a_task_whose_every_attempt_crashed_is_still_dropped(tmp_path):
    job = tmp_path / "control"
    job.mkdir()
    _attempt(job, "demo-h1", "aa", errored=True)
    _attempt(job, "demo-h1", "bb", errored=True)
    assert H.broken_tasks(job) == {"demo-h1"}


def test_a_crashed_attempts_empty_workspace_is_not_averaged_in_as_a_zero(tmp_path):
    """The crashed trial's directory exists and is empty through no fault of the agent. Averaged in
    it would pull a 3-attempt task's mean down by a third and read as the skill performing worse."""
    job = tmp_path / "control"
    job.mkdir()
    _attempt(job, "demo-h0", "aa", errored=True, answer="")
    _attempt(job, "demo-h0", "bb", answer="real work")
    answers = H.collect_answers(job, H.broken_trials(job))
    assert len(answers["demo-h0"]) == 1
    assert "real work" in answers["demo-h0"][0]


def test_runs_at_different_attempt_counts_do_not_collide(tmp_path, monkeypatch):
    """Harbor refuses a job directory whose config changed, so re-running a skill at a new -k failed
    all 13 combinations before a container started. The earlier run's trials are also the evidence a
    rescore reads, so overwriting them is worse than the collision."""
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H, "stage_skill", lambda skill: tmp_path / "staged")
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setattr(H, "build_dataset", lambda *a, **k: tmp_path / "ds")
    monkeypatch.setattr(H, "HARBOR_DIR", tmp_path / "out")
    monkeypatch.setattr(H, "collect_answers", lambda job, skip=None: {})
    monkeypatch.setattr(H, "broken_tasks", lambda job: set())
    monkeypatch.setattr(H, "broken_trials", lambda job: set())
    monkeypatch.setattr(H, "score", lambda answers, skill, holdout, skip=None: [1.0, 1.0])
    seen = []

    def fake_run_arm(dataset, agent, source, jobs_dir, job_name, *a, **k):
        seen.append(jobs_dir)
        return jobs_dir / job_name

    monkeypatch.setattr(H, "run_arm", fake_run_arm)
    H.run_harbor_eval("demo", ["claude-code"], attempts=1, log=lambda *a: None)
    H.run_harbor_eval("demo", ["claude-code"], attempts=3, log=lambda *a: None)

    roots = {path.parent.name for path in seen}
    assert roots == {"demo", "demo-k3"}


def test_a_subscription_capable_harness_will_not_quietly_bill_per_token(monkeypatch):
    """The default is silent and expensive. A whole grid ran on metered keys with both subscription
    logins sitting unused on the same host, and nothing in the output said so — the per-arm dollar
    figure Harbor prints is a computed estimate that reads the same either way."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
    monkeypatch.delenv("CLAUDE_FORCE_OAUTH", raising=False)
    monkeypatch.delenv(H.ALLOW_API_BILLING, raising=False)
    refusals = H.billing_refusals(["claude-code@anthropic/claude-opus-5"])
    assert len(refusals) == 1
    assert "CLAUDE_FORCE_OAUTH" in refusals[0] and "claude setup-token" in refusals[0]


def test_a_harness_with_no_cli_to_harness_is_not_refused(monkeypatch):
    """terminus-2, goose, aider, opencode and pi drive a provider API directly. There is no
    subscription to prefer, so an API key is inherent to running them at all."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-whatever")
    monkeypatch.delenv(H.ALLOW_API_BILLING, raising=False)
    assert H.billing_refusals(["terminus-2@anthropic/claude-opus-5", "goose@anthropic/claude-sonnet-5",
                               "aider@openai/gpt-5.5", "pi@openai/gpt-5.5"]) == []


def test_the_subscription_flag_clears_the_refusal(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-whatever")
    monkeypatch.setenv("CODEX_FORCE_AUTH_JSON", "1")
    monkeypatch.delenv(H.ALLOW_API_BILLING, raising=False)
    assert H.billing_refusals(["codex@openai/gpt-5.5"]) == []


def test_metered_billing_can_still_be_opted_into_deliberately(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
    monkeypatch.delenv("CLAUDE_FORCE_OAUTH", raising=False)
    monkeypatch.setenv(H.ALLOW_API_BILLING, "1")
    assert H.billing_refusals(["claude-code@anthropic/claude-opus-5"]) == []


def test_the_grid_refuses_to_start_rather_than_billing_then_reporting(tmp_path, monkeypatch):
    """Per-row would be too late: a grid is hours long and the bill is run up by then."""
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H, "stage_skill", lambda skill: tmp_path / "staged")
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
    monkeypatch.delenv("CLAUDE_FORCE_OAUTH", raising=False)
    monkeypatch.delenv(H.ALLOW_API_BILLING, raising=False)

    def must_not_run(*a, **k):
        raise AssertionError("a container was started before the billing check")

    monkeypatch.setattr(H, "run_arm", must_not_run)
    monkeypatch.setattr(H, "build_dataset", must_not_run)
    with pytest.raises(SystemExit, match="refusing to start"):
        H.run_harbor_eval("demo", ["claude-code@anthropic/claude-opus-5"], log=lambda *a: None)


def test_every_run_asks_for_build_and_setup_headroom(tmp_path, monkeypatch):
    """Seeded tasks each build their own image, where an unseeded dataset shared one cached image
    built once. `apt-get update && install` on an uncached image overran the 120s compose budget,
    and it surfaced as a bare RuntimeError with an empty verifier directory — a build failure that
    looks nothing like one, and which scored as a dropped task."""
    seen = []

    class Done:
        returncode, stderr, stdout = 0, "", ""

    monkeypatch.setattr(H.subprocess, "run", lambda argv, **kw: seen.append(argv) or Done())
    monkeypatch.setattr(H, "_refuse_broken_job", lambda *a: None)
    H.run_arm(tmp_path / "ds", "terminus-2", None, tmp_path / "jobs", "control", log=lambda *a: None)

    argv = seen[0]
    assert "--environment-build-timeout-multiplier" in argv
    assert float(argv[argv.index("--environment-build-timeout-multiplier") + 1]) > 1
    assert "--agent-setup-timeout-multiplier" in argv


def test_the_image_ships_what_agents_would_otherwise_install_themselves(tmp_path):
    """terminus-2 installs tmux and asciinema into the container when they are missing, and that
    apt-get overran the 120s exec budget on a cold cache — a bare RuntimeError from
    _install_recording_tools, empty verifier directory, task counted broken and dropped. Its
    installer skips the work when both are present. pytest is here because the seeded READMEs tell
    the agent to run it and Ubuntu 24.04 refuses pip installs under PEP 668."""
    dockerfile = (H.build_dataset("demo", HOLDOUT, tmp_path) / "demo-h0"
                  / "environment" / "Dockerfile").read_text()
    for package in ("tmux", "asciinema", "python3-pytest"):
        assert package in dockerfile, f"{package} must be baked in, not installed per trial"


def _local_target(alias="dell-qwen", **changes):
    models = {"dell-qwen": "dot-backbone", "spark-deepseek": "deepseek-v4-flash",
              "orin-abliterated": "ablit35b"}
    values = {
        "alias": alias,
        "display_name": alias,
        "base_url": f"http://{alias}.test:8000",
        "served_model": models[alias],
        "context_length": 32768,
        "protocols": frozenset({"chat", "responses", "messages"}),
        "family": "Qwen3.6" if alias == "dell-qwen" else "fixture-family",
        "parameter_billions": 27.0 if alias == "dell-qwen" else 1.0,
        "quantization": "fp8-published" if alias == "dell-qwen" else "fixture-quant",
        "tool_parser": "qwen3_xml" if alias == "dell-qwen" else "fixture-parser",
    }
    values.update(changes)
    return LocalTarget(**values)


def _completed_canary(job: Path, task: str, *, solution=True, exception=None, completed=1):
    (job / "result.json").parent.mkdir(parents=True, exist_ok=True)
    (job / "result.json").write_text(json.dumps({"stats": {"n_completed_trials": completed,
                                                               "n_errored_trials": 0,
                                                               "n_cancelled_trials": 0}}))
    trial = job / f"{task}__run" / "verifier" / "solution"
    trial.mkdir(parents=True)
    if solution:
        (trial / "answer.txt").write_text("done")
    record = {"task_name": f"ingot/{task}", "exception_info": None}
    if exception:
        record["exception_info"] = {"exception_type": exception}
    (trial.parent.parent / "result.json").write_text(json.dumps(record))


def test_canary_requires_a_completed_exception_free_trial_with_a_solution(tmp_path, monkeypatch):
    """A successful Harbor process with no agent deliverable must block the full sweep."""
    target = _local_target()
    job = tmp_path / "canaries" / "demo" / target.job_slug / "codex"
    _completed_canary(job, "demo-h0", solution=False)
    calls = []

    def fake_run_arm(*args, **kwargs):
        calls.append((args, kwargs))
        return job

    monkeypatch.setattr(H, "run_arm", fake_run_arm)
    record = H.run_canary("demo", tmp_path / "dataset", HOLDOUT, "/staged/demo", "codex",
                          target, tmp_path / "canaries", log=lambda *a: None)
    assert record["error"] == "canary produced no nonempty verifier solution artifact"
    route = H.gateway_route(target, "codex")
    assert route is not None
    args, kwargs = calls[0]
    assert args[3] == job.parent and args[4] == f"codex--{route.identity}"
    assert args[1] == "ingot.optimize.harbor_codex_gateway:GatewayCodex"
    assert kwargs["task_name"] == "demo-h0" and kwargs["attempts"] == 1
    assert kwargs["model"] == route.model
    assert kwargs["agent_env"]["OPENAI_BASE_URL"].startswith("http://172.17.")
    assert kwargs["process_env"]["PYTHONPATH"].split(os.pathsep)[0] == str(
        Path(H.__file__).resolve().parents[2])


def test_canary_exports_the_persisted_job_after_run_arm(tmp_path, monkeypatch):
    """Removing the canary telemetry caller would leave its retained attempt undiscoverable."""
    target = _local_target()
    job = tmp_path / "canaries" / "demo" / target.job_slug / "codex"
    _completed_canary(job, "demo-h0")
    source = tmp_path / "staged"
    skill_file = source / "demo" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("fixture skill body\n")
    events = []

    def fake_run_arm(*args, **kwargs):
        events.append(("run", job))
        return job

    def fake_export(exported_job, metadata):
        events.append(("export", exported_job, metadata))
        return [{"status": "verified"}]

    monkeypatch.setattr(H, "run_arm", fake_run_arm)
    monkeypatch.setattr(H, "export_job_attempts", fake_export)

    record = H.run_canary("demo", tmp_path / "dataset", HOLDOUT, str(source), "codex",
                          target, tmp_path / "canaries", log=lambda *a: None)

    assert [event[0] for event in events] == ["run", "export"]
    assert events[1][1] == job
    assert events[1][2]["combination"] == record["combination"]
    assert events[1][2]["arm"] == "canary"
    assert events[1][2]["skill"] == "demo"
    assert events[1][2]["task_texts"] == {"demo-h0": HOLDOUT[0]["task"],
                                           "demo-h1": HOLDOUT[1]["task"]}
    assert events[1][2]["skill_sha256"] == hashlib.sha256(
        skill_file.read_bytes()).hexdigest()
    assert events[1][2]["skill_body"] == "fixture skill body\n"
    assert record["ok"] is True and "telemetry_error" not in record


def test_telemetry_provenance_sanitizes_task_text_before_persisting(tmp_path):
    source = tmp_path / "staged"
    skill_file = source / "demo" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("fixture skill body\n")
    holdout = [{"task": (
        "Call https://private-endpoint.invalid/v1 with "
        "OPENAI_API_KEY=fixture-secret-value"
    )}]

    provenance = H._telemetry_provenance("demo", holdout, str(source))

    persisted = provenance["task_texts"]["demo-h0"]
    assert "private-endpoint.invalid" not in persisted
    assert "fixture-secret-value" not in persisted
    assert "<redacted-" in persisted


def test_canary_telemetry_failure_preserves_measurement_and_records_manifest_error(tmp_path, monkeypatch):
    """Telemetry failure must not recast a completed attempt as an agent failure or rerun it."""
    target = _local_target()
    job = tmp_path / "canaries" / "demo" / target.job_slug / "codex"
    _completed_canary(job, "demo-h0")
    result = next(job.glob("*/result.json"))
    before = result.read_bytes()
    runs = []
    source = tmp_path / "staged"
    skill_file = source / "demo" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("fixture skill body\n")
    monkeypatch.setattr(H, "run_arm", lambda *a, **k: runs.append(True) or job)
    monkeypatch.setattr(
        H, "export_job_attempts",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("read-back unavailable")),
    )

    record = H.run_canary("demo", tmp_path / "dataset", HOLDOUT, str(source), "codex",
                          target, tmp_path / "canaries", log=lambda *a: None)

    assert runs == [True]
    assert record["ok"] is True and "error" not in record
    assert record["telemetry_error"] == "RuntimeError: read-back unavailable"
    assert result.read_bytes() == before


@pytest.mark.parametrize("failure", ["skill-read", "combo-write"])
def test_canary_provenance_failures_after_run_are_telemetry_only(
        tmp_path, monkeypatch, failure):
    """Provenance preparation cannot recast a completed canary as an agent failure."""
    target = _local_target()
    job = tmp_path / "canaries" / "demo" / target.job_slug / "codex"
    _completed_canary(job, "demo-h0")
    result = next(job.glob("*/result.json"))
    before = result.read_bytes()
    source = tmp_path / "staged"
    skill_file = source / "demo" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("fixture skill body\n")
    monkeypatch.setattr(H, "run_arm", lambda *a, **k: job)
    if failure == "skill-read":
        original_read_bytes = Path.read_bytes

        def fail_skill_read(path):
            if path == skill_file:
                raise OSError("fixture skill read failed")
            return original_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", fail_skill_read)
    else:
        monkeypatch.setattr(
            H, "_write_json_atomic",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fixture combo write failed")),
        )

    record = H.run_canary(
        "demo", tmp_path / "dataset", HOLDOUT, str(source), "codex",
        target, tmp_path / "canaries", log=lambda *a: None,
    )

    assert record["ok"] is True and "error" not in record
    assert "telemetry_error" in record
    assert result.read_bytes() == before


def test_canary_requires_harbors_structured_completed_trial_count(tmp_path, monkeypatch):
    """A stale solution directory must not turn a zero-completion job into a passed routing seam."""
    target = _local_target()
    job = tmp_path / "canaries" / "demo" / target.job_slug / "codex"
    _completed_canary(job, "demo-h0", completed=0)
    monkeypatch.setattr(H, "run_arm", lambda *a, **k: job)
    record = H.run_canary("demo", tmp_path / "dataset", HOLDOUT, "/staged/demo", "codex",
                          target, tmp_path / "canaries", log=lambda *a: None)
    assert record["error"] == "canary wrote no completed trial"


def test_claude_gateway_keeps_the_parent_import_environment(tmp_path, monkeypatch):
    target = _local_target()
    job = tmp_path / "canaries" / "demo" / target.job_slug / "claude-code"
    _completed_canary(job, "demo-h0", solution=False)
    calls = []
    monkeypatch.setattr(H, "run_arm", lambda *args, **kwargs: calls.append((args, kwargs)) or job)

    H.run_canary("demo", tmp_path / "dataset", HOLDOUT, "/staged/demo", "claude-code",
                 target, tmp_path / "canaries", log=lambda *a: None)
    assert calls[0][1]["process_env"] is os.environ


def test_routed_canaries_use_revisioned_jobs_while_direct_canaries_keep_their_harness_name(tmp_path, monkeypatch):
    target = _local_target()
    seen = []

    def fake_run_arm(dataset, agent, source, jobs_dir, job_name, *args, **kwargs):
        seen.append((jobs_dir / job_name, job_name))
        _completed_canary(jobs_dir / job_name, "demo-h0", solution=False)
        return jobs_dir / job_name

    monkeypatch.setattr(H, "run_arm", fake_run_arm)
    for harness in ("claude-code", "codex", "terminus-2"):
        H.run_canary("demo", tmp_path / "dataset", HOLDOUT, "/staged/demo", harness,
                     target, tmp_path / "canaries", log=lambda *a: None)
    claude, codex, direct = seen
    assert claude[0] != codex[0]
    assert claude[1].startswith("claude-code--")
    assert codex[1].startswith("codex--")
    assert direct[1] == "terminus-2"


def test_canary_rejects_an_exception_bearing_trial(tmp_path, monkeypatch):
    """A solution copied before an agent crash is diagnostic evidence, not a passed canary."""
    target = _local_target()
    job = tmp_path / "canaries" / "demo" / target.job_slug / "codex"
    _completed_canary(job, "demo-h0", exception="AgentSetupTimeoutError")
    monkeypatch.setattr(H, "run_arm", lambda *a, **k: job)
    record = H.run_canary("demo", tmp_path / "dataset", HOLDOUT, "/staged/demo", "codex",
                          target, tmp_path / "canaries", log=lambda *a: None)
    assert record["error"] == "canary trial did not complete without an exception"
    assert "ok" not in record


def test_canary_rejects_empty_solution_files_and_nonempty_exception_artifacts(tmp_path, monkeypatch):
    target = _local_target()
    job = tmp_path / "canaries" / "demo" / target.job_slug / "codex"
    _completed_canary(job, "demo-h0")
    solution = next(job.glob("*/verifier/solution/answer.txt"))
    solution.write_bytes(b"")
    monkeypatch.setattr(H, "run_arm", lambda *a, **k: job)
    record = H.run_canary("demo", tmp_path / "dataset", HOLDOUT, "/staged/demo", "codex",
                          target, tmp_path / "canaries", log=lambda *a: None)
    assert record["error"] == "canary produced no nonempty verifier solution artifact"

    solution.write_text("done")
    (solution.parents[2] / "exception.txt").write_text("agent failed")
    record = H.run_canary("demo", tmp_path / "dataset", HOLDOUT, "/staged/demo", "codex",
                          target, tmp_path / "canaries", log=lambda *a: None)
    assert record["error"] == "canary trial wrote exception evidence"


def test_local_sweep_preflights_and_finishes_every_canary_before_any_full_arm(tmp_path, monkeypatch):
    """A target or adapter seam must fail cheaply before full evidence roots exist."""
    first, second, third = (_local_target(), _local_target("spark-deepseek"),
                            _local_target("orin-abliterated"))
    events = []
    arm_routing = {}
    arm_agents = []
    monkeypatch.setattr(H, "HARBOR_DIR", tmp_path / "harbor")
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H, "stage_skill", lambda skill: tmp_path / "staged")
    monkeypatch.setattr(H, "build_dataset", lambda *a: events.append("dataset") or tmp_path / "dataset")
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setattr(
        H, "discover_target",
        lambda alias, url: events.append(("discover", alias)) or
        {target.alias: target for target in (first, second, third)}[alias],
    )
    monkeypatch.setattr(H, "probe_protocol",
                        lambda target, protocol: events.append(("probe", target.alias, protocol)))
    monkeypatch.setattr(H, "probe_chat_tool_round_trip",
                        lambda target: events.append(("tool-probe", target.alias)))

    def fake_canary(skill, dataset, holdout, source, harness, target, root, *, exploratory, log):
        events.append(("canary", target.alias, harness))
        return {"harness": harness, "target_alias": target.alias, "ok": True}

    def fake_arm(dataset, harness, source, jobs_dir, job_name, *args, **kwargs):
        events.append(("arm", jobs_dir, job_name, kwargs))
        arm_agents.append(harness)
        arm_routing.setdefault(jobs_dir, []).append(kwargs)
        return jobs_dir / job_name

    monkeypatch.setattr(H, "run_canary", fake_canary)
    monkeypatch.setattr(H, "run_arm", fake_arm)
    monkeypatch.setattr(H, "broken_tasks", lambda job: set())
    monkeypatch.setattr(H, "broken_trials", lambda job: set())
    monkeypatch.setattr(H, "collect_answers", lambda *a, **k: pytest.fail("raw arms were collected for scoring"))
    monkeypatch.setattr(H, "score", lambda *a, **k: pytest.fail("full arms were scored inline"))

    manifest = H.run_local_sweep("demo", [first, second, third], log=lambda *a: None)

    canaries = [event for event in events if event[0] == "canary"]
    arms = [event for event in events if event[0] == "arm"]
    assert len(canaries) == len(H.LOCAL_HARNESSES) * 3 == 27
    assert events.index(canaries[-1]) < events.index(arms[0])
    assert max(i for i, event in enumerate(events) if event[0] == "discover") < events.index(canaries[0])
    assert max(i for i, event in enumerate(events) if event[0] == "probe") < events.index(canaries[0])
    assert [event for event in events if event[0] == "tool-probe"] == [
        ("tool-probe", "dell-qwen"),
        ("tool-probe", "spark-deepseek"),
        ("tool-probe", "orin-abliterated"),
    ]
    assert events.index(("tool-probe", "dell-qwen")) < events.index(canaries[0])
    assert all(event[1].parent.name == "demo-k3" for event in arms)
    expected_models = {
        (H.gateway_route(target, harness).model if H.gateway_route(target, harness)
         else H.harbor_model(target, harness))
        for target in (first, second, third) for harness in H.LOCAL_HARNESSES
    }
    assert all(event[3]["model"] in expected_models for event in arms)
    assert all(event[3]["attempts"] == 3 for event in arms)
    gateway_arms = [event for event in arms if event[3]["agent_env"].get("HARBOR_GATEWAY_CODEX_PROVIDER")]
    assert all(event[3]["process_env"]["PYTHONPATH"].split(os.pathsep)[0]
               == str(Path(H.__file__).resolve().parents[2]) for event in gateway_arms)
    claude_gateway_arms = [event for event in arms if "-claude-code-" in event[3]["model"]]
    assert all(event[3]["process_env"] is os.environ for event in claude_gateway_arms)
    assert arm_agents == [
        H.gateway_agent_name(route) if route else harness
        for target in (first, second, third) for harness in H.LOCAL_HARNESSES
        for route in (H.gateway_route(target, harness),) for _ in range(2)
    ]
    assert all(len(routings) == 2 and routings[0] == routings[1]
               for routings in arm_routing.values())
    assert manifest["aborted"] is False and len(manifest["combinations"]) == 27
    assert all(row["raw_evidence"] is True for row in manifest["combinations"].values())
    assert all(set(row).isdisjoint({"skill_mean", "control_mean", "lift", "skill_scores",
                                    "control_scores", "judge", "scoring_revision"})
               for row in manifest["combinations"].values())


def test_full_arms_export_each_persisted_job_and_keep_telemetry_errors_in_manifest(tmp_path, monkeypatch):
    """Both paid-for arms remain measured once while publication waits for missing telemetry."""
    target = _local_target()
    manifest = {"combinations": {}}
    events = []
    source = tmp_path / "staged"
    skill_file = source / "demo" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("fixture skill body\n")

    def fake_run_arm(dataset, agent, source, jobs_dir, arm, **kwargs):
        job = jobs_dir / arm
        events.append(("run", arm, job))
        return job

    def fake_export(job, metadata):
        events.append(("export", metadata["arm"], job))
        if metadata["arm"] == "skill":
            raise RuntimeError("public read-back unavailable")
        return [{"status": "verified"}]

    monkeypatch.setattr(H, "run_arm", fake_run_arm)
    monkeypatch.setattr(H, "export_job_attempts", fake_export)
    monkeypatch.setattr(H, "broken_tasks", lambda job: set())

    H._run_local_full_arms(
        "demo", tmp_path / "dataset", [target], ["codex"], HOLDOUT, str(source),
        3, 2, tmp_path / "jobs", manifest, log=lambda *a: None,
    )

    assert [(event[0], event[1]) for event in events] == [
        ("run", "skill"), ("export", "skill"),
        ("run", "control"), ("export", "control"),
    ]
    row = next(iter(manifest["combinations"].values()))
    assert row["raw_evidence"] is True
    assert row["skill_job"].endswith("/skill") and row["control_job"].endswith("/control")
    assert row["telemetry_errors"] == {"skill": "RuntimeError: public read-back unavailable"}
    assert "error" not in row
    assert row["skill"] == "demo"
    assert row["task_texts"] == {"demo-h0": HOLDOUT[0]["task"], "demo-h1": HOLDOUT[1]["task"]}
    assert row["skill_sha256"] == hashlib.sha256(skill_file.read_bytes()).hexdigest()
    assert row["skill_body"] == "fixture skill body\n"


def test_full_arm_provenance_failure_after_runs_is_telemetry_only(tmp_path, monkeypatch):
    """Both completed arms remain raw evidence when shared provenance cannot be built."""
    target = _local_target()
    manifest = {"combinations": {}}
    jobs = []

    def fake_run_arm(dataset, agent, source, jobs_dir, arm, **kwargs):
        job = jobs_dir / arm
        jobs.append(job)
        return job

    monkeypatch.setattr(H, "run_arm", fake_run_arm)
    monkeypatch.setattr(
        H, "_telemetry_provenance",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fixture provenance failed")),
    )
    monkeypatch.setattr(H, "broken_tasks", lambda job: set())

    H._run_local_full_arms(
        "demo", tmp_path / "dataset", [target], ["codex"], HOLDOUT, "/staged/demo",
        3, 2, tmp_path / "jobs", manifest, log=lambda *a: None,
    )

    assert [job.name for job in jobs] == ["skill", "control"]
    row = next(iter(manifest["combinations"].values()))
    assert row["raw_evidence"] is True and "error" not in row
    assert row["telemetry_errors"] == {"provenance": "OSError: fixture provenance failed"}


def test_failed_canary_becomes_unmeasured_combination_and_starts_no_full_arm(tmp_path, monkeypatch):
    target = _local_target()
    monkeypatch.setattr(H, "HARBOR_DIR", tmp_path / "harbor")
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H, "stage_skill", lambda skill: tmp_path / "staged")
    monkeypatch.setattr(H, "build_dataset", lambda *a: tmp_path / "dataset")
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setattr(H, "discover_target", lambda alias, url: target)
    monkeypatch.setattr(H, "probe_protocol", lambda *a: None)
    monkeypatch.setattr(H, "run_canary", lambda *a, **k: {"error": "adapter did not route"})
    monkeypatch.setattr(H, "run_arm", lambda *a, **k: pytest.fail("full arm ran after failed canary"))

    manifest = H.run_local_sweep("demo", [target], harnesses=("codex",), log=lambda *a: None)
    assert manifest["aborted"] is False
    key = f"codex@{target.served_model}--{target.job_slug}"
    assert manifest["canaries"][key]["error"] == "adapter did not route"
    assert manifest["combinations"][key]["error"] == "adapter did not route"
    combo = tmp_path / "harbor" / "jobs" / "demo-k3" / key / "combo.json"
    assert json.loads(combo.read_text())["canary_error"] == "adapter did not route"
    assert json.loads((tmp_path / "harbor" / "canaries" / "demo" / "manifest.json").read_text()) == manifest
    assert not (combo.parent / "skill").exists()
    assert not (combo.parent / "control").exists()


def test_failed_canary_skips_only_its_intersection_while_passes_run_full_arms(tmp_path, monkeypatch):
    first, second = _local_target(), _local_target("spark-deepseek")
    arms = []
    monkeypatch.setattr(H, "HARBOR_DIR", tmp_path / "harbor")
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H, "stage_skill", lambda skill: tmp_path / "staged")
    monkeypatch.setattr(H, "build_dataset", lambda *a: tmp_path / "dataset")
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setattr(H, "discover_target", lambda alias, url: {first.alias: first, second.alias: second}[alias])
    monkeypatch.setattr(H, "probe_protocol", lambda *a: None)
    monkeypatch.setattr(H, "probe_chat_tool_round_trip", lambda *a: None)
    monkeypatch.setattr(
        H, "run_canary",
        lambda skill, dataset, holdout, source, harness, target, root, *, exploratory, log:
        ({"error": "rate limited"} if target.alias == first.alias else {"ok": True}),
    )

    def fake_arm(dataset, agent, source, jobs_dir, arm, **kwargs):
        arms.append((jobs_dir.name, arm))
        return jobs_dir / arm

    monkeypatch.setattr(H, "run_arm", fake_arm)
    monkeypatch.setattr(H, "broken_tasks", lambda job: set())
    monkeypatch.setattr(H, "export_job_attempts", lambda *a, **k: [])
    monkeypatch.setattr(H, "_telemetry_provenance", lambda *a, **k: {"skill": "demo"})

    manifest = H.run_local_sweep(
        "demo", [first, second], harnesses=("aider",), log=lambda *a: None,
    )

    failed_key = H._combination_id("aider", first)
    passed_key = H._combination_id("aider", second)
    assert arms == [(passed_key, "skill"), (passed_key, "control")]
    assert manifest["combinations"][failed_key]["error"] == "rate limited"
    assert manifest["combinations"][passed_key]["raw_evidence"] is True
    assert manifest["aborted"] is False


def test_native_sweep_forwards_process_environment_to_canary_and_full_jobs(tmp_path, monkeypatch):
    target = _local_target()
    seen = []
    process_env = {"PATH": "/bin", "HARBOR_EXTRA_DOCKER_COMPOSE": "/tmp/network.yml"}
    monkeypatch.setattr(H, "HARBOR_DIR", tmp_path / "harbor")
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H, "stage_skill", lambda skill: tmp_path / "staged")
    monkeypatch.setattr(H, "build_dataset", lambda *a: tmp_path / "dataset")
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setattr(H, "discover_target", lambda alias, url: target)
    monkeypatch.setattr(H, "probe_protocol", lambda *a: None)
    monkeypatch.setattr(H, "probe_chat_tool_round_trip", lambda *a: None)
    monkeypatch.setattr(H, "_run_native_canaries", lambda *a, **k: seen.append(
        ("canary", k["process_env"])) or {H._combination_id("aider", target): {"ok": True}})
    monkeypatch.setattr(H, "_run_native_full_arms", lambda *a, **k: seen.append(
        ("full", k["process_env"])))

    manifest = H.run_local_sweep(
        "demo", [target], harnesses=("aider",), native_parallel=True,
        process_env=process_env, log=lambda *a: None,
    )

    assert manifest["aborted"] is False
    assert seen == [("canary", process_env), ("full", process_env)]


def test_colon_bearing_served_model_never_enters_full_arm_job_path(tmp_path, monkeypatch):
    target = _local_target(
        served_model="qwen3.5:9b",
        alias="dell-qwen",
    )
    manifest = {"combinations": {}}
    seen = []

    def fake_arm(dataset, agent, source, jobs_dir, arm, **kwargs):
        seen.append(jobs_dir)
        return jobs_dir / arm

    monkeypatch.setattr(H, "run_arm", fake_arm)
    monkeypatch.setattr(H, "broken_tasks", lambda job: set())
    monkeypatch.setattr(H, "export_job_attempts", lambda *a, **k: [])
    monkeypatch.setattr(H, "_telemetry_provenance", lambda *a, **k: {"skill": "demo"})

    H._run_local_full_arms(
        "demo", tmp_path / "dataset", [target], ["aider"], HOLDOUT, "/staged/demo",
        1, 2, tmp_path / "jobs", manifest, canaries={}, exploratory=True,
        log=lambda *a: None,
    )

    key = H._combination_id("aider", target)
    assert manifest["combinations"][key]["combination"] == key
    assert seen and all(":" not in job_dir.name for job_dir in seen)
    assert seen[0].name == H._combination_job_slug("aider", target)


def test_colon_free_combination_keeps_historical_job_directory_name():
    target = _local_target()
    assert H._combination_job_slug("aider", target) == H._combination_id("aider", target)


def test_persisted_canary_manifest_redacts_exception_text(tmp_path, monkeypatch):
    target = _local_target()
    monkeypatch.setenv("OPENAI_API_KEY", "manifest-secret")
    monkeypatch.setattr(H, "HARBOR_DIR", tmp_path / "harbor")
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H, "stage_skill", lambda skill: tmp_path / "staged")
    monkeypatch.setattr(H, "build_dataset", lambda *a: tmp_path / "dataset")
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setattr(H, "discover_target", lambda alias, url: target)
    monkeypatch.setattr(H, "probe_protocol", lambda *a: None)
    monkeypatch.setattr(H, "run_canary", lambda *a, **k: {
        "error": "Authorization: Basic basic-value http://target.invalid:8001 manifest-secret"})

    H.run_local_sweep("demo", [target], harnesses=("codex",), log=lambda *a: None)
    persisted = (tmp_path / "harbor" / "canaries" / "demo" / "manifest.json").read_text()
    for raw in ("basic-value", "target.invalid", "manifest-secret"):
        assert raw not in persisted


def test_canary_only_runs_preflights_and_canaries_without_creating_full_jobs(tmp_path, monkeypatch):
    """A successful routing proof must stop before it creates a scored-arm evidence root."""
    target = _local_target()
    events = []
    monkeypatch.setattr(H, "HARBOR_DIR", tmp_path / "harbor")
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H, "stage_skill", lambda skill: tmp_path / "staged")
    monkeypatch.setattr(H, "build_dataset", lambda *a: events.append("dataset") or tmp_path / "dataset")
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setattr(H, "discover_target", lambda alias, url: target)
    monkeypatch.setattr(H, "probe_protocol", lambda *a: events.append("probe"))
    monkeypatch.setattr(H, "run_canary", lambda *a, **k: events.append("canary") or {"ok": True})
    monkeypatch.setattr(H, "run_arm", lambda *a, **k: pytest.fail("full arm ran during canary-only"))

    manifest = H.run_local_sweep("demo", [target], harnesses=("codex",),
                                 canary_only=True, log=lambda *a: None)

    assert events == ["dataset", "probe", "canary"]
    assert manifest["canary_only"] is True
    assert manifest["aborted"] is False
    assert manifest["combinations"] == {}
    assert json.loads((tmp_path / "harbor" / "canaries" / "demo" / "manifest.json").read_text()) == manifest
    assert not (tmp_path / "harbor" / "jobs").exists()


def test_local_combo_metadata_identifies_endpoint_without_serializing_its_url(tmp_path, monkeypatch):
    target = _local_target(base_url="http://private-host.test:9134")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "SKILL.md").write_text("fixture skill body\n")
    monkeypatch.setattr(H, "HARBOR_DIR", tmp_path / "harbor")
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H, "stage_skill", lambda skill: staged)
    monkeypatch.setattr(H, "build_dataset", lambda *a: tmp_path / "dataset")
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setattr(H, "discover_target", lambda alias, url: target)
    monkeypatch.setattr(H, "probe_protocol", lambda *a: None)
    monkeypatch.setattr(H, "run_canary", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(H, "run_arm", lambda dataset, harness, source, jobs_dir, name, *a, **k: jobs_dir / name)
    monkeypatch.setattr(H, "broken_tasks", lambda job: {"demo-h1"} if job.name == "skill" else {"demo-h0"})
    monkeypatch.setattr(H, "collect_answers", lambda *a, **k: pytest.fail("raw evidence was collected for scoring"))
    monkeypatch.setattr(H, "score", lambda *a, **k: pytest.fail("raw evidence was scored inline"))

    manifest = H.run_local_sweep("demo", [target], harnesses=("codex",), log=lambda *a: None)
    combo = (tmp_path / "harbor" / "jobs" / "demo-k3"
             / f"codex@{target.served_model}--{target.job_slug}" / "combo.json")
    record = json.loads(combo.read_text())
    assert set(record) == {"combination", "harness", "model", "target_alias",
                           "endpoint_fingerprint", "protocol", "task_fingerprint", "attempts",
                           "gateway_revision", "gateway_identity", "gateway_agent", "skill",
                           "skill_body", "skill_sha256", "task_texts", "family",
                           "parameter_billions", "quantization", "tool_parser",
                           "exploratory", "rankable"}
    assert {field: record[field] for field in (
        "family", "parameter_billions", "quantization", "tool_parser")
    } == {"family": "Qwen3.6", "parameter_billions": 27.0,
          "quantization": "fp8-published", "tool_parser": "qwen3_xml"}
    assert record["exploratory"] is False and record["rankable"] is True
    assert record["task_fingerprint"] == H._task_fingerprint(HOLDOUT)
    assert H.SCORING_REVISION == "harbor-rubric-v2-agy"
    assert record["combination"] == f"codex@{target.served_model}--{target.job_slug}"
    assert target.base_url not in combo.read_text()
    row = next(iter(manifest["combinations"].values()))
    assert row["raw_evidence"] is True
    assert row["skill_job"].endswith("/skill")
    assert row["control_job"].endswith("/control")
    assert row["tasks_dropped"] == ["demo-h0", "demo-h1"]
    assert not {"skill_mean", "control_mean", "lift", "skill_scores", "control_scores"} & set(row)
    assert not (tmp_path / "harbor" / "demo.json").exists()


def test_local_combination_error_keeps_identity_without_measurement_fields(tmp_path, monkeypatch):
    target = _local_target()
    monkeypatch.setattr(H, "HARBOR_DIR", tmp_path / "harbor")
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H, "stage_skill", lambda skill: tmp_path / "staged")
    monkeypatch.setattr(H, "build_dataset", lambda *a: tmp_path / "dataset")
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setattr(H, "discover_target", lambda alias, url: target)
    monkeypatch.setattr(H, "probe_protocol", lambda *a: None)
    monkeypatch.setattr(H, "run_canary", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(H, "run_arm", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))

    manifest = H.run_local_sweep("demo", [target], harnesses=("codex",), log=lambda *a: None)
    row = next(iter(manifest["combinations"].values()))
    assert row["harness"] == "codex" and row["target_alias"] == target.alias
    assert "RuntimeError: down" in row["error"]
    assert not {"skill_mean", "control_mean", "lift", "skill_scores", "control_scores"} & set(row)


def test_target_cli_discovers_repeated_targets_and_constructs_the_cross_product(monkeypatch):
    discovered = []
    called = {}

    def fake_discover(alias, url):
        discovered.append((alias, url))
        return _local_target(alias)

    monkeypatch.setattr(H, "discover_target", fake_discover)
    monkeypatch.setattr(H, "run_local_sweep", lambda skill, targets, **kwargs:
                        called.update(skill=skill, targets=targets, **kwargs) or {"aborted": False})
    assert H.main(["demo", "--target", "dell-qwen=http://one.test", "--target",
                   "spark-deepseek=http://two.test", "--target",
                   "orin-abliterated=http://three.test", "--agent", "codex", "--agent", "pi"]) == 0
    assert discovered == [("dell-qwen", "http://one.test"),
                          ("spark-deepseek", "http://two.test"),
                          ("orin-abliterated", "http://three.test")]
    assert [target.alias for target in called["targets"]] == [
        "dell-qwen", "spark-deepseek", "orin-abliterated"]
    assert called["harnesses"] == ["codex", "pi"] and called["attempts"] == 3


def test_target_cli_forwards_canary_only_to_the_local_sweep(monkeypatch):
    target = _local_target()
    called = {}
    monkeypatch.setattr(H, "discover_target", lambda alias, url: target)
    monkeypatch.setattr(H, "run_local_sweep", lambda skill, targets, **kwargs:
                        called.update(skill=skill, targets=targets, **kwargs) or
                        {"aborted": False, "canary_only": True})

    assert H.main(["demo", "--target", "dell-qwen=http://one.test", "--canary-only"]) == 0
    assert called["canary_only"] is True


def test_target_cli_allows_one_attempt_only_for_explicit_exploration(monkeypatch):
    target = _local_target()
    called = {}
    monkeypatch.setattr(H, "discover_target", lambda alias, url: target)
    monkeypatch.setattr(H, "run_local_sweep", lambda skill, targets, **kwargs:
                        called.update(skill=skill, targets=targets, **kwargs) or
                        {"aborted": False})

    assert H.main(["scope-discipline", "--target", "dell-qwen=http://one.test",
                   "--agent", "aider", "--attempts", "1", "--exploratory"]) == 0
    assert called["attempts"] == 1
    assert called["exploratory"] is True


def test_local_sweep_enforces_exploratory_attempts_for_python_callers(tmp_path, monkeypatch):
    target = _local_target()
    with pytest.raises(ValueError, match="exploratory=True"):
        H.run_local_sweep("demo", [target], attempts=1, log=lambda *_args: None)

    monkeypatch.setattr(H, "HARBOR_DIR", tmp_path / "harbor")
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H, "stage_skill", lambda skill: tmp_path / "staged")
    monkeypatch.setattr(H, "build_dataset", lambda *args: tmp_path / "dataset")
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setattr(H, "discover_target", lambda alias, url: target)
    monkeypatch.setattr(H, "probe_protocol", lambda *args: None)
    monkeypatch.setattr(H, "probe_chat_tool_round_trip", lambda *args: None)
    canary_roots = []
    monkeypatch.setattr(H, "run_canary", lambda *args, **kwargs:
                        canary_roots.append(args[6]) or {"ok": True})
    manifest = H.run_local_sweep("demo", [target], harnesses=("aider",), attempts=1,
                                 exploratory=True, canary_only=True, log=lambda *_args: None)
    assert manifest["exploratory"] is True
    assert manifest["rankable"] is False
    exploratory_manifest = (tmp_path / "harbor" / "canaries-k1" / "demo" / "manifest.json")
    assert json.loads(exploratory_manifest.read_text())["rankable"] is False
    assert not (tmp_path / "harbor" / "canaries" / "demo" / "manifest.json").exists()
    assert canary_roots == [tmp_path / "harbor" / "canaries-k1"]


def test_catalog_sweep_rejects_changed_tasks_before_preflight(tmp_path, monkeypatch):
    target = _local_target()
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setattr(H, "discover_target",
                        lambda *_args: pytest.fail("endpoint preflight ran"))

    with pytest.raises(RuntimeError, match="tasks changed"):
        H.run_local_sweep("demo", [target], harnesses=("aider",),
                          expected_task_fingerprint="0" * 64, evidence_root=tmp_path)


def test_catalog_sweep_rejects_changed_route_revision_before_protocol_probe(tmp_path, monkeypatch):
    target = _local_target()
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H, "stage_skill", lambda skill: tmp_path / "source")
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setattr(H, "discover_target", lambda *_args: target)
    monkeypatch.setattr(H, "probe_protocol",
                        lambda *_args: pytest.fail("protocol probe ran"))

    manifest = H.run_local_sweep(
        "demo", [target], harnesses=("aider",), evidence_root=tmp_path,
        expected_runtime_revisions={f"route:{target.fingerprint}:aider": "stale"})

    assert manifest["aborted"] is True
    assert "changed after catalog enqueue" in manifest["preflight_error"]


def test_catalog_sweep_uses_content_addressed_evidence_root(tmp_path, monkeypatch):
    target = _local_target()
    monkeypatch.setattr(H, "load_tasks", lambda skill: ([], HOLDOUT, {}))
    monkeypatch.setattr(H, "build_dataset", lambda *args: tmp_path / "dataset")
    monkeypatch.setattr(H.shutil, "which", lambda binary: "/usr/bin/harbor")
    monkeypatch.setattr(H, "discover_target", lambda alias, url: target)
    monkeypatch.setattr(H, "probe_protocol", lambda *args: None)
    monkeypatch.setattr(H, "probe_chat_tool_round_trip", lambda *args: None)
    roots = []
    monkeypatch.setattr(H, "run_canary", lambda *args, **kwargs:
                        roots.append(args[6]) or {"ok": True})
    source = tmp_path / "source"
    source.mkdir()

    H.run_local_sweep("demo", [target], harnesses=("aider",), skill_source=str(source),
                      evidence_root=tmp_path / "content", canary_only=True)

    assert roots == [tmp_path / "content" / "canaries"]
    assert (tmp_path / "content" / "canaries" / "demo" / "manifest.json").is_file()


def test_canary_only_requires_a_local_target(capsys):
    with pytest.raises(SystemExit, match="2"):
        H.main(["demo", "--canary-only"])
    assert "--canary-only requires --target" in capsys.readouterr().err


def test_exploratory_requires_a_local_target(capsys):
    with pytest.raises(SystemExit, match="2"):
        H.main(["demo", "--exploratory", "--attempts", "1"])
    assert "--exploratory requires --target" in capsys.readouterr().err


@pytest.mark.parametrize("argv, message", [
    (["demo", "--target", "dell-qwen=http://one.test", "--model", "not-local"], "--model"),
    (["demo", "--target", "dell-qwen=http://one.test", "-k", "2"], "--attempts"),
    (["demo", "--target", "dell-qwen=http://one.test", "-k", "1"], "--exploratory"),
    (["demo", "--target", "dell-qwen=http://one.test", "-k", "3", "--exploratory"],
     "--exploratory requires --attempts 1"),
])
def test_target_cli_rejects_incompatible_local_options(argv, message, capsys):
    with pytest.raises(SystemExit, match="2"):
        H.main(argv)
    assert message in capsys.readouterr().err


def test_legacy_cli_path_remains_the_proprietary_evaluator(monkeypatch):
    called = {}
    monkeypatch.setattr(H, "run_harbor_eval", lambda skill, agents, **kwargs:
                        called.update(skill=skill, agents=agents, **kwargs) or {})
    assert H.main(["demo", "--agent", "claude-code", "--model", "gpt-test", "-k", "1"]) == 0
    assert called == {"skill": "demo", "agents": ["claude-code"], "model": "gpt-test",
                      "concurrency": 2, "attempts": 1, "log": print}


def test_unified_job_answer_collection_filters_exact_native_arm(tmp_path):
    from ingot.optimize.harbor_native import NativeTrialIdentity, identity_env

    common = dict(
        combination_id="aider@dot-backbone--dell-qwen-deadbeefcafe",
        endpoint_fingerprint="deadbeefcafe", harness="aider", protocol="chat",
        gateway_revision="direct",
    )
    skill = NativeTrialIdentity(**common, arm="skill")
    control = NativeTrialIdentity(**common, arm="control")
    for index, (identity, answer) in enumerate(((skill, "skill answer"),
                                                (control, "control answer"))):
        trial = tmp_path / f"demo-h0__trial-{index}"
        solution = trial / "verifier" / "solution"
        solution.mkdir(parents=True)
        (solution / "answer.md").write_text(answer)
        (trial / "result.json").write_text(json.dumps({"task_name": "ingot/demo-h0"}))
        (trial / "lock.json").write_text(json.dumps({"agent": {"env": identity_env(identity)}}))

    assert H.collect_answers(tmp_path, identity=skill) == {"demo-h0": [
        "--- answer.md ---\nskill answer"]}
    assert H.collect_answers(tmp_path, identity=control) == {"demo-h0": [
        "--- answer.md ---\ncontrol answer"]}


def test_unified_job_answer_collection_rejects_symlinked_solution(tmp_path):
    from ingot.optimize.harbor_native import NativeTrialIdentity, identity_env

    identity = NativeTrialIdentity(
        combination_id="aider@dot-backbone--dell-qwen-deadbeefcafe",
        endpoint_fingerprint="deadbeefcafe", harness="aider", protocol="chat",
        gateway_revision="direct", arm="skill")
    trial = tmp_path / "demo-h0__trial"
    verifier = trial / "verifier"
    verifier.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "answer.md").write_text("external")
    (verifier / "solution").symlink_to(outside, target_is_directory=True)
    (trial / "result.json").write_text(json.dumps({"task_name": "ingot/demo-h0"}))
    (trial / "lock.json").write_text(json.dumps({"agent": {"env": identity_env(identity)}}))

    with pytest.raises(ValueError, match="solution directory"):
        H.collect_answers(tmp_path, identity=identity)
