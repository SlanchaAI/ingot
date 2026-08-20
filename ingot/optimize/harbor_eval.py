"""Sandboxed cross-harness skill evaluation, built on Harbor.

`compat.py` answers "does this skill body help this *model*", using one bare completion per task.
This answers "does it help this *harness*" — the real CLI agent, with its own system prompt, tool
loop and configured model, running unrestricted inside a fresh container that Harbor provisions,
injects the agent into, and tears down.

Harbor (https://github.com/harbor-framework/harbor, Apache-2.0) owns the parts that are not ours:
the per-task container, ~30 CLI agent adapters (claude-code, codex, pi, goose, gemini-cli, aider,
opencode, cursor-cli, openhands, …), concurrency, and trajectory capture. It also ships Terminus-2,
a neutral harness that gives any model the same shell loop — which is the only way to vary the model
without also varying the harness, since `claude` serves only Anthropic models and `codex` only
OpenAI.

What is ours is the experiment: a skill body is a *treatment*. Every task runs twice, once with the
body injected into the harness's system prompt and once without it, and lift is the difference. The
same fixed Ingot judge grades both arms, so a harness cannot flatter itself.

The judge runs OUTSIDE the sandbox, over artifacts the verifier copies into `/logs/verifier/`.
That keeps the judge prompt in one place instead of duplicated into every task image, and keeps the
judge's API key out of a container that is running an agent in yolo mode.

Usage:  python -m ingot.optimize.harbor_eval <skill> --agent claude-code [--agent codex] [--model M]
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import resolve_skill_dir
from .ab import load_tasks
from .harbor_targets import (LocalTarget, discover_target, harbor_agent_kwargs, harbor_model,
                             local_agent_env, parse_target, probe_chat_tool_round_trip,
                             probe_protocol, protocol_for,
                             scrub_provider_env)
from .harbor_gateway import (GatewaySession, gateway_agent_env, gateway_agent_name, gateway_process_env,
                             gateway_metadata, gateway_route)
from .harbor_langfuse import EXPORTER_REVISION, export_job_attempts
from .harbor_native import (NativeCell, NativeTrialIdentity, compile_canary_job,
                            compile_measurement_job,
                            identity_env, identity_from_env, iter_attempt_dirs, native_trial_identity,
                            NATIVE_TRIAL_MEMORY_MB,
                            select_measurement_cells, write_job_config)
from .harbor_redaction import _redact_harbor_receipt_output, _redact_persisted
from .judge import judge
from ingot import paths

HARBOR_DIR = paths.runs() / "harbor"
BUILD_DIR = HARBOR_DIR / "datasets"
HARBOR_BIN = os.environ.get("HARBOR_BIN", "harbor")
LOCAL_HARNESSES = (
    "claude-code", "terminus-2", "goose", "opencode", "openclaw",
    "mini-swe-agent", "codex", "aider", "pi",
)

# The agent works in a container, not a chat window, so the deliverable is a file it produced. This
# is the whole reason to run in a sandbox rather than judge a completion: the harness has to
# actually do the work.
SOLUTION_DIR = "/app/solution"
REPO_DIR = "/app/repo"
_INSTRUCTION_SUFFIX = f"""

---

Write your complete deliverable into `{SOLUTION_DIR}/` (create the directory if it does not exist).
Anything outside that directory is discarded and will not be graded.
"""

# A process skill has nothing to bite on in an empty container. "Verify in the execution context",
# "feed the guard the input it must reject" and "wire it to the real caller" are all unanswerable
# without existing code to read, run, and change — which is why the first task set could not
# separate any arm from any other. A task may seed a working tree; the agent edits it in place.
_SEEDED_SUFFIX = f"""

---

An existing project is checked out at `{REPO_DIR}/`. Work in it directly.

When you are done, copy every file you changed or created, plus your evidence, into
`{SOLUTION_DIR}/` (create it if needed), preserving the paths they have in the project.
Only `{SOLUTION_DIR}/` is graded.
"""

# tmux and asciinema are here because terminus-2 installs them into the container itself when they
# are missing, and that apt-get overran the 120s exec budget on a cold cache — surfacing as a bare
# "RuntimeError: Command timed out after 120 seconds" from _install_recording_tools, with an empty
# verifier directory, counted as a broken task and dropped. Its installer skips the work entirely
# when both are already present.
#
# It is also a fairness fix. Harnesses differ in how much they install before they can start, and a
# harness whose setup is heavier was losing whole tasks for it. That is a measurement of apt, not of
# the harness. pytest is here for the same reason: the seeded tasks' own READMEs tell the agent to
# run it, and Ubuntu 24.04 refuses `pip install` under PEP 668, so every agent would otherwise spend
# its budget discovering that.
_DOCKERFILE = """FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends \\
        python3 python3-pip python3-pytest git curl ca-certificates tmux asciinema \\
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
"""

_SEEDED_DOCKERFILE = _DOCKERFILE + f"""COPY seed/ {REPO_DIR}/
RUN find {REPO_DIR} -name '*.sh' -exec chmod +x {{}} +
"""

# The verifier does not grade. It copies what the agent produced somewhere Harbor persists, and
# always returns 1: a real reward here would be a second, unfixed grader competing with the Ingot
# judge, and the two would disagree.
_TEST_SH = f"""#!/bin/bash
mkdir -p /logs/verifier/solution
cp -r {SOLUTION_DIR}/. /logs/verifier/solution/ 2>/dev/null || true
echo 1 > /logs/verifier/reward.txt
"""

# An agent's own evidence log is a claim about what it ran, and a claim is exactly what a skill that
# rewards writing evidence logs teaches it to produce. `verify` runs the project's real check after
# the agent is gone and captures the result, so the judge has one outcome signal from outside the
# answer being graded. It is captured evidence, not the reward: the reward stays fixed at 1 so the
# Ingot judge remains the only grader.
_VERIFY_SH = """#!/bin/bash
mkdir -p /logs/verifier/solution
cp -r {solution}/. /logs/verifier/solution/ 2>/dev/null || true
out=/logs/verifier/solution/_objective_check.txt
{{
  echo "Ran by the harness after the agent finished, in {repo}, not by the agent:"
  printf '  $ %s\\n' {quoted}
  echo "---"
  cd {repo} 2>/dev/null && timeout 120 bash -lc {quoted}
  echo "--- exit=$?"
}} > "$out" 2>&1
echo 1 > /logs/verifier/reward.txt
"""


def _task_name(skill: str, index: int) -> str:
    return f"{skill}-h{index}"


def _write_seed(files: dict | None, seed_dir: Path) -> bool:
    """Write a task's seeded working tree under the image build context. True if anything was written.

    Paths are confined to `seed_dir`: a task file is authored data, but `../..` in a key would write
    outside the dataset and silently corrupt this checkout rather than the container's."""
    if not isinstance(files, dict) or not files:
        return False
    seed_dir.mkdir(parents=True, exist_ok=True)
    root = seed_dir.resolve()
    for relative, content in files.items():
        target = (seed_dir / str(relative)).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"seeded file path escapes the task directory: {relative!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    return True


def build_dataset(skill: str, holdout: list[dict], out_dir: Path) -> Path:
    """Write a Harbor dataset with one task per held-out task. Returns the dataset directory.

    Rebuilt from scratch each time: a stale task left behind from an earlier holdout would be run
    and scored as though it were part of this skill's current eval set."""
    dataset = out_dir / skill
    if dataset.exists():
        shutil.rmtree(dataset)
    (dataset).mkdir(parents=True)

    entries = []
    for index, task in enumerate(holdout):
        name = _task_name(skill, index)
        root = dataset / name
        (root / "environment").mkdir(parents=True)
        (root / "tests").mkdir(parents=True)
        seeded = _write_seed(task.get("files"), root / "environment" / "seed")
        (root / "instruction.md").write_text(
            task["task"] + (_SEEDED_SUFFIX if seeded else _INSTRUCTION_SUFFIX), encoding="utf-8")
        (root / "environment" / "Dockerfile").write_text(
            _SEEDED_DOCKERFILE if seeded else _DOCKERFILE, encoding="utf-8")
        test_sh = root / "tests" / "test.sh"
        verify = str(task.get("verify") or "").strip()
        test_sh.write_text(
            _VERIFY_SH.format(solution=SOLUTION_DIR, repo=REPO_DIR, quoted=shlex.quote(verify))
            if seeded and verify else _TEST_SH, encoding="utf-8")
        test_sh.chmod(0o755)
        (root / "task.toml").write_text(
            'schema_version = "1.3"\n'
            "artifacts = []\n\n"
            "[task]\n"
            f'name = "ingot/{name}"\n'
            f'description = "held-out eval task {index} for skill {skill}"\n'
            "authors = []\n"
            "keywords = []\n\n"
            "[metadata]\n\n"
            "[verifier]\n"
            "timeout_sec = 300.0\n"
            "collect = []\n\n"
            "[verifier.env]\n\n"
            "[agent]\n"
            "timeout_sec = 900.0\n\n"
            "[environment]\n"
            # The agent needs the network to reach its own model provider. Containment here is the
            # container, not the network: that is what makes yolo mode acceptable.
            'network_mode = "public"\n'
            "build_timeout_sec = 600.0\n"
            'os = "linux"\n'
            "mcp_servers = []\n\n"
            "[environment.env]\n\n"
            "[solution.env]\n",
            encoding="utf-8")
        entries.append(f'[[tasks]]\nname = "ingot/{name}"\n')

    (dataset / "dataset.toml").write_text(
        "[dataset]\n"
        f'name = "ingot/{skill}"\n'
        f'description = "Ingot held-out eval tasks for skill {skill}"\n'
        "authors = []\n"
        "keywords = []\n\n" + "\n".join(entries), encoding="utf-8")
    return dataset


def stage_skill(skill: str) -> Path:
    """A directory holding exactly one `<name>/SKILL.md`, for Harbor's Agent Skills loader.

    Not the skill's parent directory: the vault holds every other skill beside it, and handing the
    loader that whole tree would put 70-odd unrelated skills in front of the agent. The two arms
    have to differ by exactly one skill or lift measures the library, not the skill."""
    staged = BUILD_DIR / "staged" / skill
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True)
    shutil.copytree(resolve_skill_dir(skill), staged / skill)
    return staged


# Harnesses that are a CLI with its own subscription login, and the flag that makes Harbor use it.
# Harbor's adapters default to the API key and only take the subscription when told: claude-code
# keeps ANTHROPIC_API_KEY unless CLAUDE_FORCE_OAUTH is set (and prefers the key when both are
# present), codex keeps OPENAI_API_KEY unless CODEX_FORCE_AUTH_JSON is. Every other harness here is
# a generic model-caller with no CLI to harness, so an API key is inherent to running it at all.
SUBSCRIPTION_HARNESSES = {
    "claude-code": ("CLAUDE_FORCE_OAUTH", "ANTHROPIC_API_KEY", "claude setup-token"),
    "codex": ("CODEX_FORCE_AUTH_JSON", "OPENAI_API_KEY", "codex login"),
}
ALLOW_API_BILLING = "HARBOR_ALLOW_API_BILLING"

# Headroom for the image build and the agent install, both of which a seeded dataset makes heavier.
# Overridable because the right value depends on the host's network and how cold its build cache is.
BUILD_TIMEOUT_MULTIPLIER = float(os.environ.get("HARBOR_BUILD_TIMEOUT_MULTIPLIER", "4"))
SETUP_TIMEOUT_MULTIPLIER = float(os.environ.get("HARBOR_SETUP_TIMEOUT_MULTIPLIER", "3"))


def billing_refusals(agents: list[str]) -> list[str]:
    """Harnesses about to bill per token when a subscription login was available.

    Fail-closed on purpose. The default is silent and expensive: a whole grid ran on metered API
    keys with both subscription credentials sitting unused on the same host, and nothing in the
    output said so — the per-arm dollar figure Harbor prints is a computed estimate and reads the
    same either way. Set HARBOR_ALLOW_API_BILLING=1 to opt in deliberately."""
    if os.environ.get(ALLOW_API_BILLING, "").strip().lower() in ("1", "true", "yes"):
        return []
    refusals = []
    for entry in agents:
        harness = entry.partition("@")[0]
        pair = SUBSCRIPTION_HARNESSES.get(harness)
        if not pair:
            continue
        flag, key, how = pair
        if os.environ.get(flag, "").strip() or not os.environ.get(key, "").strip():
            continue
        refusals.append(f"{entry}: would run on {key} (metered) rather than its subscription. "
                        f"Set {flag}=1 after `{how}`.")
    return refusals


def _without_langfuse_env(values: Mapping[str, str] | None) -> dict[str, str]:
    return {key: value for key, value in (values or {}).items() if not key.startswith("LANGFUSE_")}


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    """Avoid exposing a partially written receipt or canary manifest to an interrupted reader."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _write_harbor_invocation_receipt(jobs_dir: Path, job_name: str, done: subprocess.CompletedProcess,
                                     parent: Mapping[str, str]) -> None:
    """Persist only non-sensitive Harbor boundary facts, including zero exits before a trial."""
    job = jobs_dir / job_name
    # Harbor normally creates this directory.  An early CLI failure has no job state, so create
    # only this expected receipt location rather than fabricating a trial or measurement.
    _write_json_atomic(job / "harbor-invocation.json", {
        "returncode": done.returncode,
        # Arbitrary Harbor output can contain a provider header, argv, or raw endpoint in forms a
        # redactor cannot soundly enumerate. Counts prove the process boundary without persisting
        # any of that material.
        "stdout_bytes": len(str(done.stdout or "").encode("utf-8")),
        "stderr_bytes": len(str(done.stderr or "").encode("utf-8")),
        "stdout_excerpt": _redact_harbor_receipt_output(done.stdout, parent),
        "stderr_excerpt": _redact_harbor_receipt_output(done.stderr, parent),
    })


def run_arm(dataset: Path, agent: str, skill_source: str | None, jobs_dir: Path, job_name: str,
            model: str | None = None, concurrency: int = 2, attempts: int = 1, *,
            agent_env: Mapping[str, str] | None = None,
            agent_kwargs: Mapping[str, str] | None = None,
            task_name: str | None = None,
            process_env: Mapping[str, str] | None = None,
            log=print) -> Path:
    """Run every task in the dataset through one harness, with or without the skill. Returns job dir.

    The treatment is Harbor's own `--skill`, which implements the Agent Skills spec: the skill
    directory is mounted into the environment and the harness discovers `SKILL.md` itself. That is
    how a skill actually reaches an agent in production, and it applies identically to every
    adapter — pasting the body into a system prompt for one harness and a recipe for another would
    make the comparison measure the injection channel as much as the skill.

    `skill_source` is a local path or a git source (`org/name[@ref]`), so the benchmark can be
    pointed at exactly the bytes the canonical vault publishes.
    """
    argv = [HARBOR_BIN, "run", "--path", str(dataset), "--agent", agent,
            "--n-concurrent", str(concurrency), "--jobs-dir", str(jobs_dir),
            "--job-name", job_name]
    if attempts > 1:
        argv += ["--n-attempts", str(attempts)]
    # Seeded tasks each build their own image, because their seed differs. Before seeding, every
    # task in a dataset shared one identical Dockerfile and so one cached image built once; now
    # there are as many builds as tasks, and `apt-get update && install` on an uncached image
    # overran the 120s compose budget. Observed as `RuntimeError: Command timed out after 120
    # seconds` with an empty verifier directory and `docker inspect returned 1` in the trial log —
    # a build failure that looks nothing like one.
    argv += ["--environment-build-timeout-multiplier", str(BUILD_TIMEOUT_MULTIPLIER),
             "--agent-setup-timeout-multiplier", str(SETUP_TIMEOUT_MULTIPLIER)]
    if skill_source:
        argv += ["--skill", skill_source]
    if model:
        argv += ["--model", model]
    # Harbor forwards these repeated options to the adapter. Sort keys so an identical local
    # target produces identical command evidence regardless of mapping insertion order.
    agent_env = _without_langfuse_env(agent_env)
    for key in sorted(agent_env):
        argv += ["--ae", f"{key}={agent_env[key]}"]
    for key in sorted(agent_kwargs or {}):
        value = agent_kwargs[key]
        rendered = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
        argv += ["--ak", f"{key}={rendered}"]
    if task_name:
        # Harbor filters local datasets by the task directory basename, not dataset.toml's
        # namespaced task label. The latter looks right but matches no local task.
        argv += ["--include-task-name", task_name]
    log(f"[harbor] {agent:<14} running {dataset.name} ({job_name} arm)")
    run_kwargs = {"capture_output": True, "text": True}
    if process_env is None:
        # Legacy/nonlocal runs need inherited provider auth, but Langfuse remains parent-only.
        run_kwargs["env"] = _without_langfuse_env(os.environ)
    else:
        # Some Harbor adapters read their routing settings before they construct the
        # container command. Preserve only the explicit, local adapter settings here;
        # inherited provider credentials remain stripped at this process boundary.
        run_kwargs["env"] = _without_langfuse_env(scrub_provider_env(process_env))
    run_kwargs["env"].update(agent_env)
    done = subprocess.run(argv, **run_kwargs)
    _write_harbor_invocation_receipt(jobs_dir, job_name, done,
                                     process_env if process_env is not None else os.environ)
    if done.returncode != 0:
        raise RuntimeError(f"harbor run failed for {agent}: {done.stderr.strip()[-600:]}")
    job = jobs_dir / job_name
    _refuse_broken_job(job, agent, job_name)
    return job


def watch_native_job(job: Path, expected: Mapping[NativeTrialIdentity, Mapping[str, int]], on_ready,
                     *, released: set[NativeTrialIdentity] | None = None
                     ) -> set[NativeTrialIdentity]:
    """Release exact identities only after their complete terminal attempt set is persisted."""
    released = set(released or ())
    if not job.is_dir():
        return released
    for identity, required in expected.items():
        observed = {task: 0 for task in required}
        for attempt in iter_attempt_dirs(job, identity=identity):
            try:
                record = json.loads((attempt / "result.json").read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            if not record.get("finished_at"):
                continue
            task = str(record.get("task_name") or "").split("/")[-1].split("__")[0]
            if task not in observed:
                raise RuntimeError(f"{identity.combination_id} {identity.arm} wrote unexpected task {task}")
            observed[task] += 1
        if any(observed[task] > count for task, count in required.items()):
            raise RuntimeError(f"{identity.combination_id} {identity.arm} exceeded expected attempts")
        if observed == dict(required) and identity not in released:
            if on_ready(identity) is not False:
                released.add(identity)
    return released


def _process_start_token(pid: int) -> str | None:
    """Bind an owner receipt to one process lifetime, not a reusable PID."""
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        return proc_stat.read_text().split()[21]
    except (OSError, IndexError):
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                              text=True)
        token = done.stdout.strip()
        return token or None


def _claim_native_owner(path: Path, config: Path) -> None:
    owner = {"pid": os.getpid(), "start_token": _process_start_token(os.getpid()),
             "config": str(config)}
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                existing = json.loads(path.read_text())
                pid = existing.get("pid")
                token = existing.get("start_token")
            except (OSError, ValueError, AttributeError):
                raise RuntimeError("native Harbor owner receipt is unreadable")
            if (isinstance(pid, int) and isinstance(token, str)
                    and _process_start_token(pid) == token):
                raise RuntimeError(f"native Harbor job already has live owner PID {pid}")
            path.unlink()
            continue
        with os.fdopen(descriptor, "w") as handle:
            json.dump(owner, handle)
            handle.flush()
            os.fsync(handle.fileno())
        return


def run_native_job(config: Path, jobs_dir: Path, job_name: str,
                   expected: Mapping[NativeTrialIdentity, Mapping[str, int]], *, on_ready,
                   process_env: Mapping[str, str], poll_seconds: float = 0.5,
                   allow_completed_reuse: bool = False) -> Path:
    """Run one Harbor config and publish complete identity slices while siblings continue."""
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job = jobs_dir / job_name
    log_path = jobs_dir / f"{job_name}.harbor.log"
    owner_path = jobs_dir / f"{job_name}.owner.json"
    state_path = jobs_dir / f"{job_name}.released.json"
    _claim_native_owner(owner_path, config)
    argv = [HARBOR_BIN, "run", "--config", str(config),
            "--override-memory-mb", str(NATIVE_TRIAL_MEMORY_MB), "--job-name", job_name]
    env = _without_langfuse_env(scrub_provider_env(process_env))
    extra_compose = env.pop("HARBOR_EXTRA_DOCKER_COMPOSE", None)
    if extra_compose:
        overlay = Path(extra_compose)
        if not overlay.is_file():
            raise RuntimeError("Harbor Docker Compose overlay is missing")
        argv.extend(["--extra-docker-compose", str(overlay)])
    # Aider checks provider presence in the Harbor parent before building its container command.
    # These are local sentinels; endpoint URLs remain isolated in each agent configuration.
    env.update({"OPENAI_API_KEY": "local", "ANTHROPIC_API_KEY": "local",
                "CODEX_API_KEY": "local"})
    process = None
    try:
        released: set[NativeTrialIdentity] = set()
        if allow_completed_reuse:
            if state_path.is_file():
                state = json.loads(state_path.read_text())
                released = {identity_from_env(item) for item in state.get("released", [])}
            # Harbor 0.20 redacts credential-shaped agent env values in persisted TrialConfigs. A
            # second `harbor run` then compares those placeholders with the resolved plan and
            # rejects an otherwise identical completed job. Released state is not enough on its
            # own: require the complete terminal artifact set before skipping the subprocess.
            terminal = watch_native_job(job, expected, lambda _identity: True)
            if released == terminal == set(expected):
                return job
            if terminal == set(expected):
                released = watch_native_job(job, expected, on_ready, released=released)
                _write_json_atomic(state_path, {"released": [identity_env(item)
                                                             for item in sorted(released, key=repr)]})
                if released == terminal:
                    return job
                raise RuntimeError("native Harbor finalization is pending")
        with log_path.open("a", encoding="utf-8") as output:
            process = subprocess.Popen(argv, stdout=output, stderr=subprocess.STDOUT, env=env,
                                       text=True)
            try:
                if not allow_completed_reuse and state_path.is_file():
                    state = json.loads(state_path.read_text())
                    released = {identity_from_env(item) for item in state.get("released", [])}
                while process.poll() is None:
                    before = set(released)
                    released = watch_native_job(job, expected, on_ready, released=released)
                    if released != before:
                        _write_json_atomic(state_path, {"released": [identity_env(item)
                                                                     for item in sorted(released, key=repr)]})
                    time.sleep(poll_seconds)
                returncode = process.wait()
                released = watch_native_job(job, expected, on_ready, released=released)
                _write_json_atomic(state_path, {"released": [identity_env(item)
                                                             for item in sorted(released, key=repr)]})
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
    finally:
        owner_path.unlink(missing_ok=True)
    if returncode != 0:
        raise RuntimeError(f"native Harbor job exited {returncode}; see {log_path}")
    missing = set(expected) - released
    if missing:
        raise RuntimeError(f"native Harbor job ended before {len(missing)} identity slice(s) completed")
    return job


def _refuse_broken_job(job: Path, agent: str, arm: str) -> None:
    """Fail an arm only when nothing in it ran.

    `harbor run` exits 0 even when trials error, and a crashed trial leaves an empty solution
    directory that `score` reads as a legitimate 0.0. Observed live: a control arm whose four trials
    were all killed during `docker compose up` scored 0.000 against a skill arm's 0.750 and reported
    `lift +0.750` — fabricated, and exactly the failure `compat.py` already guards against.

    Failing the whole arm on *any* broken trial is the opposite mistake: a single transient
    container failure then discards three good trials and the paid-for opposite arm. Individual
    broken tasks are dropped instead, by `broken_tasks`, from both arms at once."""
    result = job / "result.json"
    if not result.is_file():
        raise RuntimeError(f"{agent} {arm} arm wrote no result.json at {job}")
    stats = (json.loads(result.read_text()) or {}).get("stats") or {}
    ran = (stats.get("n_completed_trials", 0) or 0)
    broken = (stats.get("n_errored_trials", 0) or 0) + (stats.get("n_cancelled_trials", 0) or 0)
    if ran and broken >= ran:
        raise RuntimeError(f"{agent} {arm} arm had every one of its {ran} trial(s) error or "
                           f"cancel; refusing to score it")


def _trial_outcomes(job: Path, identity: NativeTrialIdentity | None = None) -> list[tuple[str, str, bool]]:
    """(trial directory name, task name, ok) for every trial in this arm."""
    out = []
    results = ([attempt / "result.json" for attempt in iter_attempt_dirs(job, identity=identity)]
               if identity is not None else job.glob("*/result.json"))
    for result in results:
        try:
            record = json.loads(result.read_text()) or {}
        except (OSError, ValueError):
            continue
        # Harbor nests this as exception_info.exception_type, and names the task in `task_name`
        # (as "ingot/<task>"). Reading a top-level `exception_type` finds nothing, which made this
        # guard a silent no-op: opencode's skill arm lost two tasks to AgentSetupTimeoutError and
        # they were scored as two 0.0s, turning an install timeout into "lift -0.375".
        failure = ((record.get("exception_info") or {}).get("exception_type") or "").strip()
        name = str(record.get("task_name") or result.parent.name).split("/")[-1]
        out.append((result.parent.name, name.split("__")[0], not failure))
    return out


def broken_trials(job: Path, identity: NativeTrialIdentity | None = None) -> set[str]:
    """Trial directory names that errored or were cancelled.

    With more than one attempt per task these have to be excluded individually. A crashed attempt
    leaves an empty solution directory, and an empty directory is scored as a real zero — so one
    flaky attempt out of three would pull the task's mean down by a third and read as the skill
    performing worse."""
    return {trial for trial, _, ok in _trial_outcomes(job, identity) if not ok}


def broken_tasks(job: Path, identity: NativeTrialIdentity | None = None) -> set[str]:
    """Task names with no surviving attempt in this arm.

    A task that crashed in one arm has to be dropped from *both*, or the arms are scored on
    different task sets and the difference between them stops being lift. But with several attempts
    per task, dropping the task because one attempt broke discards the attempts that did run — and
    they are the whole reason for paying for repeats."""
    outcomes = _trial_outcomes(job, identity)
    survivors = {task for _, task, ok in outcomes if ok}
    return {task for _, task, _ in outcomes} - survivors


def collect_answers(job_dir: Path, skip_trials: set[str] | None = None, *,
                    identity: NativeTrialIdentity | None = None) -> dict[str, list[str]]:
    """The text each task's agent left in the solution directory, keyed by task name.

    `skip_trials` drops individual crashed attempts, whose workspaces are empty through no fault of
    the agent and would otherwise be averaged in as zeros.

    A list per task, not a string: with `--n-attempts` above 1 a task has several trials, and
    keying a single answer by task name silently kept only whichever was read last — throwing away
    exactly the repeated measurements that were paid for to average the agent's own variance out.

    A task that produced nothing maps to "" rather than being dropped: an empty workspace is a real
    result (the harness ran and delivered nothing), and silently omitting it would raise the arm's
    mean by removing its own failures."""
    skip_trials = skip_trials or set()
    answers: dict[str, list[str]] = {}
    solutions = ([attempt / "verifier" / "solution"
                  for attempt in iter_attempt_dirs(job_dir, identity=identity)]
                 if identity is not None else sorted(job_dir.rglob("verifier/solution")))
    for solution in solutions:
        if identity is not None:
            verifier = solution.parent
            try:
                verifier_info = verifier.lstat()
                solution_info = solution.lstat()
            except OSError:
                continue
            if (not stat.S_ISDIR(verifier_info.st_mode)
                    or not stat.S_ISDIR(solution_info.st_mode)):
                raise ValueError("native Harbor solution directory is not a real directory")
        elif not solution.is_dir():
            continue
        if solution.parent.parent.name in skip_trials:
            continue
        name = _trial_task_name(solution)
        parts = []
        for path in sorted(p for p in solution.rglob("*") if p.is_file()):
            if identity is not None:
                relative = path.relative_to(solution)
                current = solution
                for part in relative.parts:
                    current = current / part
                    if stat.S_ISLNK(current.lstat().st_mode):
                        raise ValueError("native Harbor solution contains a symlink")
            if not _is_deliverable(path.relative_to(solution)):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue   # a binary the agent happened to leave behind is not the deliverable
            parts.append(f"--- {path.relative_to(solution)} ---\n{text}")
        answers.setdefault(name, []).append("\n\n".join(parts)[:60000])
    return answers


# Build leavings, not deliverables. The first real container run wrote __pycache__/*.pyc beside
# solution.py, and those bytes went into the text handed to the judge — noise the judge pays for
# and can be misled by.
_IGNORED_DIRS = {"__pycache__", ".git", "node_modules", ".venv", ".pytest_cache", ".mypy_cache"}


def _is_deliverable(relative: Path) -> bool:
    return not set(relative.parts) & _IGNORED_DIRS


def _trial_task_name(solution: Path) -> str:
    """The task name for a `<trial>/verifier/solution` directory.

    Harbor names the trial `<task>__<run-suffix>` (observed: `probe-h0__suyygRM`) so repeated
    attempts at one task cannot collide. The suffix has to come off, or no trial ever matches the
    task it came from and every score silently reads as a zero."""
    return solution.parent.parent.name.split("__")[0]


def score(answers: dict[str, list[str]], skill: str, holdout: list[dict],
          skip: set[str] | None = None, concurrency: int = 1) -> list[float]:
    """Judge each held-out task's collected artifacts with the fixed Ingot judge.

    A task's score is the mean over its attempts. Measured directly on this eval: re-judging one
    fixed answer three times returned an identical 0.278 every time, while re-running the same
    agent on the same task under the same model moved the score from 0.278 to 0.556. The variance
    is the agent's, not the judge's, so the remedy is repeated attempts rather than a better grader.

    An arm that delivered nothing for *every* task is refused rather than scored. A trial can
    "complete" while its agent never worked: the verifier always reports success, so an agent that
    died on its first API call still counts as a completed trial with an empty workspace. Observed
    live: aider v0.86.2 sends `temperature`, claude-sonnet-5 rejects it as deprecated, and the arm
    came back completed-and-empty — which would have scored a clean 0.000 and read as "aider is
    terrible at this skill" rather than "aider never ran". Some tasks empty is a real failure and
    still scores zero; all tasks empty is a broken combination."""
    skip = skip or set()
    kept = [i for i in range(len(holdout)) if _task_name(skill, i) not in skip]
    if not kept:
        raise RuntimeError("every task crashed in one arm or the other; nothing comparable is left")
    if not any(any(answers.get(_task_name(skill, i)) or []) for i in kept):
        raise RuntimeError(f"every task returned an empty workspace; the harness produced no "
                           f"deliverable at all, refusing to score it as zeros")
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        raise ValueError("score concurrency must be a positive integer")
    graded: dict[int, list[float]] = {index: [] for index in kept}
    jobs = []
    for index in kept:
        task = holdout[index]
        attempts = answers.get(_task_name(skill, index)) or [""]
        for answer in attempts:
            if not answer:
                graded[index].append(0.0)  # ran, produced nothing: a real zero
                continue
            jobs.append((index, task, answer))

    def grade(item) -> tuple[int, float]:
        index, task, answer = item
        # The task's own checklist, not the judge's generic four. Dropping it here is what made
        # the first build-loop matrix unreadable: controls piled up at 0.849.
        value = judge(task["task"], task["rubric"], answer,
                      check=task.get("check"), deliverable=task.get("deliverable"),
                      checklist=task.get("checklist"))["score"]
        return index, value

    if concurrency == 1 or len(jobs) < 2:
        results = map(grade, jobs)
        for index, value in results:
            graded[index].append(value)
    else:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(jobs))) as pool:
            for index, value in pool.map(grade, jobs):
                graded[index].append(value)
    return [sum(graded[index]) / len(graded[index]) for index in kept]


# Changes to how persisted Harbor artifacts are interpreted must change this identifier.  Rescore
# uses it to refuse evidence made under different scoring semantics rather than mixing the rows.
SCORING_REVISION = "harbor-rubric-v2-agy"


def _task_fingerprint(holdout: list[dict]) -> str:
    """Stable identity of the exact held-out task set, independent of dict insertion order."""
    canonical = json.dumps(holdout, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _combination_id(harness: str, target: LocalTarget) -> str:
    """Stable local evidence identity, including the endpoint fingerprint through its job slug."""
    return f"{harness}@{target.served_model}--{target.job_slug}"


def _combination_job_slug(harness: str, target: LocalTarget) -> str:
    """Keep the evidence identity exact without putting Docker-unsafe model IDs in bind paths."""
    identity = _combination_id(harness, target)
    return identity if ":" not in identity else f"{harness}@{target.job_slug}"


def _native_full_job_name(cell: NativeCell) -> str:
    """Stable one-cell Harbor boundary; changing the rest of a matrix never changes its config."""
    return f"native-full--{cell.harness}--{cell.target.job_slug}"


def _round_robin_endpoints(cells: Sequence[NativeCell]) -> list[NativeCell]:
    """Keep the next bounded cell jobs on different physical endpoint identities."""
    buckets: dict[str, list[NativeCell]] = {}
    for cell in cells:
        buckets.setdefault(cell.target.fingerprint, []).append(cell)
    ordered = []
    while any(buckets.values()):
        for bucket in buckets.values():
            if bucket:
                ordered.append(bucket.pop(0))
    return ordered


def _canary_artifact(job: Path, task_name: str,
                     identity: NativeTrialIdentity | None = None) -> str | None:
    """Return a diagnostic when the one-task canary did not yield usable Harbor evidence."""
    if identity is not None:
        attempts = list(iter_attempt_dirs(job, identity=identity))
        if len(attempts) != 1:
            return "canary wrote no completed trial result"
        result = attempts[0] / "result.json"
        try:
            record = json.loads(result.read_text()) or {}
        except (OSError, ValueError):
            return "canary trial result was unreadable"
        recorded_task = str(record.get("task_name") or "").split("/")[-1].split("__")[0]
        exception = ((record.get("exception_info") or {}).get("exception_type") or "").strip()
        if recorded_task != task_name or exception:
            return "canary trial did not complete without an exception"
        exception_path = result.parent / "exception.txt"
        if exception_path.is_file() and exception_path.read_bytes().strip():
            return "canary trial wrote exception evidence"
        answers = collect_answers(job, identity=identity)
        if not any(answer.strip() for values in answers.values() for answer in values):
            return "canary produced no nonempty verifier solution artifact"
        return None
    try:
        summary = json.loads((job / "result.json").read_text()) or {}
        completed = ((summary.get("stats") or {}).get("n_completed_trials", 0) or 0)
    except (OSError, ValueError):
        return "canary wrote no completed trial result"
    if not isinstance(completed, int) or isinstance(completed, bool) or completed < 1:
        return "canary wrote no completed trial"
    trials = list(job.glob("*/result.json"))
    if not trials:
        return "canary wrote no completed trial result"
    for result in trials:
        try:
            record = json.loads(result.read_text()) or {}
        except (OSError, ValueError):
            return "canary trial result was unreadable"
        recorded_task = str(record.get("task_name") or "").split("/")[-1].split("__")[0]
        exception = ((record.get("exception_info") or {}).get("exception_type") or "").strip()
        if recorded_task != task_name or exception:
            return "canary trial did not complete without an exception"
        exception_path = result.parent / "exception.txt"
        if exception_path.is_file() and exception_path.read_bytes().strip():
            return "canary trial wrote exception evidence"
        solution = result.parent / "verifier" / "solution"
        if not solution.is_dir() or not any(path.is_file() and path.stat().st_size > 0
                                            for path in solution.rglob("*")):
            return "canary produced no nonempty verifier solution artifact"
        return None
    return "canary wrote no matching held-out trial"


def run_canary(skill: str, dataset: Path, holdout: list[dict], source: str, harness: str,
               target: LocalTarget, canary_root: Path, *, exploratory: bool = False, log=print) -> dict:
    """Run the first held-out task once and retain the diagnostic evidence for this seam."""
    task_name = _task_name(skill, 0)
    jobs_dir = canary_root / skill / target.job_slug
    route = gateway_route(target, harness)
    # Preserve failed native/gateway evidence.  A translation revision changes the gateway model
    # but Harbor's fixed harness job name otherwise reopens the prior one-task job.
    job_name = f"{harness}--{route.identity}" if route else harness
    model = route.model if route else harbor_model(target, harness)
    record = {"combination": _combination_id(harness, target), "harness": harness,
              "model": model, "target_alias": target.alias,
              "endpoint_fingerprint": target.fingerprint, "protocol": protocol_for(harness),
              "job": str(jobs_dir / job_name), "family": target.family,
              "parameter_billions": target.parameter_billions,
              "quantization": target.quantization, "tool_parser": target.tool_parser,
              "exploratory": exploratory, "rankable": not exploratory}
    if route:
        record.update(gateway_metadata(route))
    try:
        job = run_arm(
            dataset, gateway_agent_name(route) if route else harness, source, jobs_dir, job_name, model=model, concurrency=1,
            attempts=1,
            agent_env=_without_langfuse_env(
                gateway_agent_env(target, route) if route else local_agent_env(target, harness)),
            agent_kwargs={} if route else harbor_agent_kwargs(target, harness), task_name=task_name,
            process_env=gateway_process_env(os.environ) if route and route.harness == "codex" else os.environ, log=log,
        )
    except Exception as error:  # noqa: BLE001 - retain failed seam evidence and stop before full arms
        record["error"] = f"{type(error).__name__}: {error}"[:400]
        return record
    try:
        telemetry_metadata = {key: value for key, value in record.items() if key != "job"}
        telemetry_metadata.update(_telemetry_provenance(skill, holdout, source))
        _write_json_atomic(job / "combo.json", telemetry_metadata)
        export_job_attempts(job, {**telemetry_metadata, "arm": "canary"})
    except Exception as error:  # noqa: BLE001 - measurement survives telemetry repair work
        record["telemetry_error"] = _redact_harbor_receipt_output(
            f"{type(error).__name__}: {error}", os.environ)
    try:
        if diagnostic := _canary_artifact(job, task_name):
            record["error"] = diagnostic
        else:
            record["ok"] = True
    except Exception as error:  # noqa: BLE001 - retain failed seam evidence and stop before full arms
        record["error"] = f"{type(error).__name__}: {error}"[:400]
    return record


def _run_native_canaries(skill: str, dataset: Path, targets: Sequence[LocalTarget],
                         harnesses: Sequence[str], holdout: list[dict], source: str,
                         canary_root: Path, *, global_limit: int, endpoint_limit: int,
                         allow_completed_reuse: bool = False,
                         process_env: Mapping[str, str] = os.environ, log=print) -> dict:
    """Run every skill-specific model×harness canary through one bounded Harbor job."""
    cells = [NativeCell(target, harness) for target in targets for harness in harnesses]
    jobs_root = canary_root / skill
    job_name = "native-canaries"
    config = compile_canary_job(
        dataset, _task_name(skill, 0), cells, Path(source), jobs_root,
        global_limit=global_limit,
        endpoint_limits={cell.target.fingerprint: endpoint_limit for cell in cells})
    config_path = jobs_root / "native-canaries.config.json"
    write_job_config(config_path, config)
    job = jobs_root / job_name
    records = {}
    provenance = _telemetry_provenance(skill, holdout, source)
    expected = {}
    for cell in cells:
        identity = native_trial_identity(cell.target, cell.harness, "canary")
        expected[identity] = {_task_name(skill, 0): 1}
        route = gateway_route(cell.target, cell.harness)
        record = {**_combo_metadata(holdout, cell.harness, cell.target, 1),
                  "model": route.model if route else harbor_model(cell.target, cell.harness),
                  "job": str(job), "exploratory": False, "rankable": True}
        if route:
            record.update(gateway_metadata(route))
        record["gateway_revision"] = identity.gateway_revision
        records[cell.combination_id] = record

    def on_ready(identity: NativeTrialIdentity) -> bool:
        record = records[identity.combination_id]
        metadata = {key: value for key, value in record.items() if key != "job"}
        metadata.update(provenance)
        try:
            export_job_attempts(job, {**metadata, "arm": "canary"}, identity=identity)
        except Exception as error:  # noqa: BLE001 - retain canary evidence
            record["telemetry_error"] = _redact_harbor_receipt_output(
                f"{type(error).__name__}: {error}", os.environ)
            record["error"] = "canary telemetry receipt was not verified"
            return False
        record.pop("telemetry_error", None)
        record.pop("error", None)
        diagnostic = _canary_artifact(job, _task_name(skill, 0), identity)
        if diagnostic:
            record["error"] = diagnostic
        else:
            record["ok"] = True
        return True

    process_env = gateway_process_env(process_env) if any(
        cell.harness == "codex" and gateway_route(cell.target, cell.harness) for cell in cells
    ) else process_env
    run_native_job(config_path, jobs_root, job_name, expected, on_ready=on_ready,
                   process_env=process_env, allow_completed_reuse=allow_completed_reuse)
    # A prior controller may have persisted terminal/released trials before it returned the
    # manifest. Re-finalize those exact identities from disk; exporter receipts are idempotent.
    for identity in expected:
        record = records[identity.combination_id]
        if "ok" not in record and "error" not in record:
            on_ready(identity)
    return records


def _telemetry_provenance(skill: str, holdout: list[dict], source: str) -> dict:
    skill_file = Path(source) / skill / "SKILL.md"
    if not skill_file.is_file():
        skill_file = Path(source) / "SKILL.md"
    skill_bytes = skill_file.read_bytes()
    skill_body = skill_bytes.decode("utf-8")
    return {
        "skill": skill,
        "skill_body": skill_body,
        "skill_sha256": hashlib.sha256(skill_bytes).hexdigest(),
        "task_texts": {_task_name(skill, index): _redact_harbor_receipt_output(
            str(task.get("task") or ""), {})
                       for index, task in enumerate(holdout)},
    }


def _combo_metadata(holdout: list[dict], harness: str, target: LocalTarget,
                    attempts: int, exploratory: bool = False) -> dict:
    metadata = {
        "combination": _combination_id(harness, target),
        "harness": harness,
        "model": target.served_model,
        "target_alias": target.alias,
        "endpoint_fingerprint": target.fingerprint,
        "protocol": protocol_for(harness),
        "task_fingerprint": _task_fingerprint(holdout),
        "attempts": attempts,
        "family": target.family,
        "parameter_billions": target.parameter_billions,
        "quantization": target.quantization,
        "tool_parser": target.tool_parser,
        "exploratory": exploratory,
        "rankable": not exploratory,
    }
    if route := gateway_route(target, harness):
        metadata.update(gateway_metadata(route))
    return metadata


def _run_local_full_arms(skill: str, dataset: Path, targets: Sequence[LocalTarget],
                         harnesses: Sequence[str], holdout: list[dict], source: str,
                         attempts: int, concurrency: int,
                         jobs_root: Path, manifest: dict, canaries: dict | None = None,
                         exploratory: bool = False, log=print) -> None:
    """Run full arms for passed canaries and persist failed seams as unmeasured rows."""
    for target in targets:
        for harness in harnesses:
            route = gateway_route(target, harness)
            metadata = _combo_metadata(holdout, harness, target, attempts, exploratory)
            key = _combination_id(harness, target)
            jobs_dir = jobs_root / _combination_job_slug(harness, target)
            canary = (canaries or {}).get(key, {})
            if "error" in canary:
                error = _redact_harbor_receipt_output(str(canary["error"]), os.environ)[:400]
                jobs_dir.mkdir(parents=True, exist_ok=True)
                _write_json_atomic(jobs_dir / "combo.json", {**metadata, "canary_error": error})
                manifest["combinations"][key] = {**metadata, "error": error}
                log(f"[harbor] {key:<54} UNMEASURED: {error[:300]}")
                continue
            try:
                jobs_dir.mkdir(parents=True, exist_ok=True)
                jobs = {}
                routing = {
                    "model": route.model if route else harbor_model(target, harness),
                    "concurrency": concurrency, "attempts": attempts,
                    "agent_env": _without_langfuse_env(
                        gateway_agent_env(target, route) if route else local_agent_env(target, harness)),
                    "agent_kwargs": {} if route else harbor_agent_kwargs(target, harness),
                    "process_env": (gateway_process_env(os.environ)
                                    if route and route.harness == "codex" else os.environ), "log": log,
                }
                telemetry_errors = {}
                telemetry_ready: bool | None = None
                for arm in ("skill", "control"):
                    jobs[arm] = run_arm(dataset, gateway_agent_name(route) if route else harness,
                                        source if arm == "skill" else None,
                                        jobs_dir, arm, **routing)
                    if telemetry_ready is None:
                        try:
                            metadata.update(_telemetry_provenance(skill, holdout, source))
                            _write_json_atomic(jobs_dir / "combo.json", metadata)
                            telemetry_ready = True
                        except Exception as error:  # noqa: BLE001 - retain paid-for arms
                            telemetry_ready = False
                            telemetry_errors["provenance"] = _redact_harbor_receipt_output(
                                f"{type(error).__name__}: {error}", os.environ)
                    if telemetry_ready:
                        try:
                            export_job_attempts(jobs[arm], {**metadata, "arm": arm})
                        except Exception as error:  # noqa: BLE001 - publication gates later
                            telemetry_errors[arm] = _redact_harbor_receipt_output(
                                f"{type(error).__name__}: {error}", os.environ)
                skipped = broken_tasks(jobs["skill"]) | broken_tasks(jobs["control"])
                manifest["combinations"][key] = {
                    **metadata, "raw_evidence": True,
                    "skill_job": str(jobs["skill"]), "control_job": str(jobs["control"]),
                    "tasks_dropped": sorted(skipped),
                }
                if telemetry_errors:
                    manifest["combinations"][key]["telemetry_errors"] = telemetry_errors
            except Exception as error:  # noqa: BLE001 - other combinations remain useful evidence
                manifest["combinations"][key] = {
                    **metadata, "error": f"{type(error).__name__}: {error}"[:400],
                }
                log(f"[harbor] {key:<54} UNAVAILABLE: {str(error)[:300]}")


def _run_native_full_arms(skill: str, dataset: Path, targets: Sequence[LocalTarget],
                          harnesses: Sequence[str], holdout: list[dict], source: str,
                          jobs_root: Path, manifest: dict,
                          canaries: Mapping[str, Mapping[str, object]], *,
                          global_limit: int, endpoint_limit: int,
                          publish_root: Path = HARBOR_DIR,
                          allow_completed_reuse: bool = False,
                          process_env: Mapping[str, str] = os.environ, log=print) -> None:
    """Run approved cells in bounded independent Harbor jobs and publish each complete pair."""
    from .harbor_rescore import current_scoring_identity, rescore

    cells = [NativeCell(target, harness) for target in targets for harness in harnesses]
    selected, unmeasured = select_measurement_cells(cells, canaries)
    manifest["combinations"].update(unmeasured)
    provenance = _telemetry_provenance(skill, holdout, source)
    unmeasured_paths = []
    for cell in cells:
        failed = unmeasured.get(cell.combination_id)
        if failed is None:
            continue
        combo = jobs_root / _combination_job_slug(cell.harness, cell.target)
        _write_json_atomic(combo / "combo.json", {
            **_combo_metadata(holdout, cell.harness, cell.target, 3),
            **provenance,
            "canary_error": failed["error"],
        })
        unmeasured_paths.append(combo)
    if not selected:
        return
    scoring = current_scoring_identity()
    combos = {}
    agent_identity = {
        "skill_sha256": provenance["skill_sha256"],
        "task_fingerprint": _task_fingerprint(holdout),
        "attempts": 3,
        "exporter_revision": EXPORTER_REVISION,
        "cells": sorted((cell.combination_id,
                         native_trial_identity(cell.target, cell.harness, "skill").gateway_revision)
                        for cell in selected),
    }
    pipeline_path = jobs_root / "native-full.pipeline.json"
    pipeline = {"agent_identity": agent_identity, "scoring_identity": scoring,
                "exported": {}, "graded": [], "published": []}
    if pipeline_path.is_file():
        saved = json.loads(pipeline_path.read_text())
        if isinstance(saved, dict):
            if saved.get("agent_identity") == agent_identity:
                pipeline["exported"] = saved.get("exported", {})
                if saved.get("scoring_identity") == scoring:
                    pipeline["graded"] = saved.get("graded", [])
                    pipeline["published"] = saved.get("published", [])
    ready = {key: set(value) for key, value in pipeline["exported"].items()}
    failed_this_run: set[tuple[str, str]] = set()
    unmeasured_pending = bool(unmeasured_paths)
    prepared = []
    for cell in _round_robin_endpoints(selected):
        combo = jobs_root / _combination_job_slug(cell.harness, cell.target)
        job_name = _native_full_job_name(cell)
        identities = {}
        expected = {}
        for arm in ("skill", "control"):
            identity = native_trial_identity(cell.target, cell.harness, arm)
            identities[arm] = identity
            expected[identity] = {_task_name(skill, index): 3 for index in range(len(holdout))}
        prepared.append((cell, combo, job_name, identities, expected))

    legacy_job_name = "native-full"
    legacy_expected = {identity: required for _cell, _combo, _job, _identities, expected in prepared
                       for identity, required in expected.items()}
    legacy_terminal = watch_native_job(
        jobs_root / legacy_job_name, legacy_expected, lambda _identity: True)
    adopted: list[NativeTrialIdentity] = []
    for cell, combo, job_name, identities, expected in prepared:
        try:
            prior = json.loads((combo / "combo.json").read_text())
        except (OSError, ValueError):
            prior = {}
        prior = prior if isinstance(prior, dict) else {}
        metadata = {**prior, **_combo_metadata(holdout, cell.harness, cell.target, 3),
                    **provenance, "native_identities": {
                        arm: identity_env(identity) for arm, identity in identities.items()}}
        metadata["gateway_revision"] = identities["skill"].gateway_revision
        native_jobs = dict(metadata.get("native_jobs") or {})
        source_jobs = {}
        missing = {}
        for arm, identity in identities.items():
            if identity in legacy_terminal:
                source_jobs[arm] = legacy_job_name
                native_jobs[arm] = legacy_job_name
                adopted.append(identity)
            else:
                source_jobs[arm] = job_name
                missing[identity] = expected[identity]
        if native_jobs:
            metadata["native_jobs"] = native_jobs
        _write_json_atomic(combo / "combo.json", metadata)
        config_path = None
        if missing:
            config = compile_measurement_job(
                dataset, [_task_name(skill, index) for index in range(len(holdout))], [cell],
                Path(source), jobs_root, attempts=3, global_limit=endpoint_limit,
                endpoint_limits={cell.target.fingerprint: endpoint_limit},
                arms=tuple(identity.arm for identity in missing))
            config_path = jobs_root / f"{job_name}.config.json"
            write_job_config(config_path, config)
        combos[cell.combination_id] = (
            combo, metadata, identities, job_name, config_path, missing, source_jobs)

    def has_recovered_lift(identity: NativeTrialIdentity, rows: Mapping[str, Any]) -> bool:
        return any(
            isinstance(row, dict) and row.get("combination") == identity.combination_id
            and row.get("endpoint_fingerprint") == identity.endpoint_fingerprint
            and row.get("skill_sha256") == agent_identity["skill_sha256"]
            and row.get("task_fingerprint") == agent_identity["task_fingerprint"]
            and row.get("attempts") == agent_identity["attempts"]
            and row.get("harness") == identity.harness
            and row.get("protocol") == identity.protocol
            and row.get("gateway_revision", "direct") == identity.gateway_revision
            and all(row.get(key) == value for key, value in scoring.items())
            and isinstance(row.get("lift"), (int, float))
            and not isinstance(row.get("lift"), bool)
            for row in rows.values()
        )

    matrix_output = publish_root / f"{skill}.rescored.json"
    if unmeasured_pending and matrix_output.is_file():
        try:
            existing = json.loads(matrix_output.read_text())
        except (OSError, ValueError):
            existing = {}
        rows = existing.get("combinations", {}) if isinstance(existing, dict) else {}
        if any(has_recovered_lift(identities["skill"], rows)
               for _combo, _metadata, identities, _job, _config, _expected, _sources
               in combos.values()):
            rescore(skill, jobs_roots=[jobs_root], combination_paths=unmeasured_paths,
                    output=matrix_output, scoring_identity=scoring, log=log)
            unmeasured_pending = False

    state_lock = threading.Lock()
    rescore_lock = threading.Lock()
    combo_locks = {key: threading.Lock() for key in combos}

    def on_ready(identity: NativeTrialIdentity) -> None:
        nonlocal unmeasured_pending
        with combo_locks[identity.combination_id]:
            combo, metadata, _identities, job_name, _config, _expected, source_jobs = combos[
                identity.combination_id]
            native_jobs = metadata.setdefault("native_jobs", {})
            native_jobs[identity.arm] = source_jobs[identity.arm]
            if set(native_jobs) >= {"skill", "control"}:
                metadata.pop("native_job", None)
            _write_json_atomic(combo / "combo.json", metadata)
            with state_lock:
                arms = ready.setdefault(identity.combination_id, set())
            if identity.arm not in arms:
                stage = (identity.combination_id, f"export:{identity.arm}")
                with state_lock:
                    if stage in failed_this_run:
                        return False
                try:
                    export_job_attempts(jobs_root / source_jobs[identity.arm],
                                        {**metadata, "arm": identity.arm},
                                        identity=identity)
                except Exception as error:  # noqa: BLE001 - retry on a later controller run
                    with state_lock:
                        failed_this_run.add(stage)
                        manifest["combinations"].setdefault(identity.combination_id, {}).update(
                            telemetry_error=f"{type(error).__name__}: {error}"[:400])
                    return False
                with state_lock:
                    arms.add(identity.arm)
                    pipeline["exported"][identity.combination_id] = sorted(arms)
                    _write_json_atomic(pipeline_path, pipeline)
            if arms == {"skill", "control"}:
                with state_lock:
                    needs_grade = identity.combination_id not in pipeline["graded"]
                if needs_grade:
                    stage = (identity.combination_id, "grade")
                    with state_lock:
                        if stage in failed_this_run:
                            return False
                    try:
                        with rescore_lock:
                            existing = (json.loads(matrix_output.read_text())
                                        if matrix_output.is_file() else {})
                            rows = (existing.get("combinations", {})
                                    if isinstance(existing, dict) else {})
                            recovered = has_recovered_lift(identity, rows)
                            with state_lock:
                                include_unmeasured = unmeasured_pending
                            if not recovered or include_unmeasured:
                                selected_paths = ([*unmeasured_paths, combo]
                                                  if include_unmeasured else [combo])
                                rescore(skill, jobs_roots=[jobs_root],
                                        combination_paths=selected_paths,
                                        output=matrix_output, scoring_identity=scoring, log=log)
                                with state_lock:
                                    unmeasured_pending = False
                    except Exception as error:  # noqa: BLE001 - retry later without rerunning agents
                        with state_lock:
                            failed_this_run.add(stage)
                            manifest["combinations"].setdefault(identity.combination_id, {}).update(
                                scoring_error=f"{type(error).__name__}: {error}"[:400])
                        return False
                    with state_lock:
                        pipeline["graded"].append(identity.combination_id)
                        _write_json_atomic(pipeline_path, pipeline)
                with state_lock:
                    needs_publish = identity.combination_id not in pipeline["published"]
                if needs_publish:
                    try:
                        with state_lock:
                            manifest["combinations"][identity.combination_id] = {
                                **metadata, "raw_evidence": True,
                                "native_jobs": {arm: str(jobs_root / source_job)
                                                for arm, source_job in source_jobs.items()}}
                            _write_json_atomic(jobs_root / "progress.json", manifest)
                    except Exception as error:  # noqa: BLE001 - grade receipt prevents repeated billing
                        with state_lock:
                            manifest["combinations"].setdefault(identity.combination_id, {}).update(
                                publication_error=f"{type(error).__name__}: {error}"[:400])
                        return False
                    with state_lock:
                        pipeline["published"].append(identity.combination_id)
                        _write_json_atomic(pipeline_path, pipeline)
        return True

    process_env = gateway_process_env(process_env) if any(
        cell.harness == "codex" and gateway_route(cell.target, cell.harness) for cell in selected
    ) else process_env
    endpoint_locks = {cell.target.fingerprint: threading.Lock() for cell in selected}

    for identity in adopted:
        if on_ready(identity) is False:
            raise RuntimeError("legacy native Harbor finalization is pending")

    def run_cell(item) -> None:
        _combo, _metadata, identities, job_name, config_path, expected, _sources = item
        if not expected:
            return
        assert config_path is not None
        fingerprint = identities["skill"].endpoint_fingerprint
        with endpoint_locks[fingerprint]:
            run_native_job(config_path, jobs_root, job_name, expected, on_ready=on_ready,
                           process_env=process_env, allow_completed_reuse=allow_completed_reuse)

    # Each one-cell Harbor job can consume at most endpoint_limit slots. Bound the number of live
    # jobs so their aggregate cannot exceed the caller's global limit. Harbor then schedules only
    # that cell's 24 trials, producing publishable evidence before later cells finish.
    workers = max(1, min(len(combos), global_limit // endpoint_limit))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(run_cell, combos.values()))


def run_local_sweep(skill: str, targets: list[LocalTarget], *,
                    harnesses: Sequence[str] = LOCAL_HARNESSES, concurrency: int = 2,
                    attempts: int = 3, skill_source: str | None = None, canary_only: bool = False,
                    exploratory: bool = False, native_parallel: bool = False,
                    evidence_root: Path | None = None,
                    expected_task_fingerprint: str | None = None,
                    expected_runtime_revisions: Mapping[str, str] | None = None,
                    global_concurrency: int | None = None,
                    endpoint_concurrency: int | None = None,
                    publish_root: Path = HARBOR_DIR,
                    content_addressed_resume: bool = False,
                    process_env: Mapping[str, str] | None = None, log=print) -> dict:
    """Evaluate every local target/harness pair whose routing canary passes.

    This deliberately returns raw-evidence manifest only.  `harbor_rescore` owns publication of
    the visible matrix, so a half-finished local sweep cannot replace a known-good matrix.
    """
    if attempts != 3 and not (exploratory and attempts == 1):
        raise ValueError("local Harbor sweeps require 3 attempts, or 1 with exploratory=True")
    if not targets:
        raise ValueError("local Harbor sweep needs at least one target")
    harnesses = tuple(harnesses)
    for harness in harnesses:
        protocol_for(harness)
    _, holdout, _ = load_tasks(skill)
    if not holdout:
        raise SystemExit(f"'{skill}' has no held-out eval tasks to run.")
    task_fingerprint = _task_fingerprint(holdout)
    if expected_task_fingerprint is not None and task_fingerprint != expected_task_fingerprint:
        raise RuntimeError(f"{skill} held-out tasks changed after catalog enqueue")
    if not shutil.which(HARBOR_BIN):
        raise SystemExit(f"'{HARBOR_BIN}' is not on PATH; install with `uv tool install harbor`.")
    source = skill_source or str(stage_skill(skill))
    dataset = build_dataset(skill, holdout, BUILD_DIR)
    manifest = {"skill": skill, "tasks": len(holdout), "attempts": attempts,
                "canary_only": canary_only, "canaries": {}, "combinations": {},
                "aborted": False, "exploratory": exploratory, "rankable": not exploratory}
    run_root = evidence_root or HARBOR_DIR
    canary_root = run_root / ("canaries-k1" if exploratory else "canaries")
    manifest_path = canary_root / skill / "manifest.json"
    global_limit = global_concurrency if global_concurrency is not None else max(16, concurrency)
    endpoint_limit = endpoint_concurrency if endpoint_concurrency is not None else max(1, concurrency)
    native_process_env = process_env if process_env is not None else os.environ
    if (not isinstance(global_limit, int) or isinstance(global_limit, bool) or global_limit < 1
            or not isinstance(endpoint_limit, int) or isinstance(endpoint_limit, bool)
            or endpoint_limit < 1 or endpoint_limit > global_limit):
        raise ValueError("native concurrency limits must be positive and endpoint <= global")

    def finish() -> dict:
        _write_json_atomic(manifest_path, _redact_persisted(manifest, os.environ))
        return manifest

    # Re-discover even targets supplied through the Python API.  CLI callers already do this while
    # parsing `--target`, but the sweep is also a public orchestration interface and must not trust
    # a hand-constructed LocalTarget to have passed the `/v1/models` identity/context preflight.
    # No container trial or full job directory exists yet.
    try:
        targets = [discover_target(target.alias, target.base_url) for target in targets]
        if expected_runtime_revisions is not None:
            for target in targets:
                for harness in harnesses:
                    identity = native_trial_identity(target, harness, "skill")
                    key = f"route:{target.fingerprint}:{harness}"
                    observed = f"{identity.protocol}/{identity.gateway_revision}/context={target.context_length}"
                    if expected_runtime_revisions.get(key) != observed:
                        raise RuntimeError(f"{key} changed after catalog enqueue")
        # Probe every required adapter protocol for every target before spending one container
        # trial. A local endpoint is part of the treatment identity; provider fallback is forbidden.
        required_protocols = sorted({protocol_for(harness) for harness in harnesses})
        for target in targets:
            for protocol in required_protocols:
                probe_protocol(target, protocol)
            if "chat" in required_protocols:
                probe_chat_tool_round_trip(target)
    except Exception as error:  # noqa: BLE001 - endpoint preflight is diagnostic, not a measurement
        manifest["aborted"] = True
        manifest["preflight_error"] = f"{type(error).__name__}: {error}"[:400]
        return finish()

    routes = [(route, target) for target in targets for harness in harnesses
              if (route := gateway_route(target, harness)) is not None]
    try:
        gateway_context = (GatewaySession(routes, canary_root / "gateway" / skill)
                           if routes else contextlib.nullcontext())
        with gateway_context:
            if native_parallel and attempts == 3 and not exploratory:
                manifest["canaries"].update(_run_native_canaries(
                    skill, dataset, targets, harnesses, holdout, source, canary_root,
                    global_limit=global_limit, endpoint_limit=endpoint_limit,
                    allow_completed_reuse=content_addressed_resume,
                    process_env=native_process_env, log=log))
                if any("telemetry_error" in record for record in manifest["canaries"].values()):
                    manifest["telemetry_pending"] = True
            else:
                for target in targets:
                    for harness in harnesses:
                        key = _combination_id(harness, target)
                        manifest["canaries"][key] = run_canary(
                            skill, dataset, holdout, source, harness, target, canary_root,
                            exploratory=exploratory, log=log)
            if canary_only:
                return finish()
            jobs_root = run_root / "jobs" / f"{skill}-k{attempts}"
            if native_parallel and attempts == 3 and not exploratory:
                _run_native_full_arms(
                    skill, dataset, targets, harnesses, holdout, source, jobs_root, manifest,
                    manifest["canaries"], global_limit=global_limit,
                    endpoint_limit=endpoint_limit, publish_root=publish_root,
                    allow_completed_reuse=content_addressed_resume,
                    process_env=native_process_env, log=log)
            else:
                _run_local_full_arms(skill, dataset, targets, harnesses, holdout, source,
                                     attempts, concurrency, jobs_root, manifest,
                                     canaries=manifest["canaries"], exploratory=exploratory, log=log)
    except Exception as error:  # noqa: BLE001 - fail before Harbor if the fixed gateway is stale/unreachable
        manifest["aborted"] = True
        manifest["gateway_error"] = f"{type(error).__name__}: {error}"[:400]
        return finish()
    return finish()


def run_harbor_eval(skill: str, agents: list[str], model: str | None = None,
                    concurrency: int = 2, skill_source: str | None = None, attempts: int = 1,
                    log=print) -> dict:
    """Skill-vs-control lift for one skill across several harnesses. Writes runs/harbor/<skill>.json.

    `skill_source` overrides where the skill is read from — a git source pins the benchmark to the
    bytes the canonical vault publishes rather than whatever this checkout happens to hold.

    `attempts` runs each task that many times per arm and averages. One attempt per task is not
    enough to see an effect this size: two control-arm runs of an identical configuration moved a
    task's score by 0.278 and swapped the ranking of two harnesses, which is larger than any lift
    the first grid reported."""
    _, holdout, _ = load_tasks(skill)
    if not holdout:
        raise SystemExit(f"'{skill}' has no held-out eval tasks to run.")
    source = skill_source or str(stage_skill(skill))
    if not shutil.which(HARBOR_BIN):
        raise SystemExit(f"'{HARBOR_BIN}' is not on PATH; install with `uv tool install harbor`.")
    # Before anything is spent, not per-row after: a grid is hours long and the bill is already run
    # up by the time a row would report it.
    if refusals := billing_refusals(agents):
        raise SystemExit("[harbor] refusing to start; these would bill per token:\n  "
                         + "\n  ".join(refusals)
                         + f"\nOr set {ALLOW_API_BILLING}=1 to accept metered billing.")
    dataset = build_dataset(skill, holdout, BUILD_DIR)
    # Runs at different attempt counts keep separate roots. Harbor refuses a job directory whose
    # config has changed ("cannot be resumed with a different config"), so re-running an existing
    # skill at a new -k failed every combination before a single container started; and the earlier
    # run's trials are the raw evidence a rescore reads, so overwriting them is worse than the
    # collision. Both runs now sit side by side.
    jobs_root = HARBOR_DIR / "jobs" / (skill if attempts == 1 else f"{skill}-k{attempts}")
    log(f"[harbor] '{skill}': {len(holdout)} held-out tasks × {len(agents)} harness(es), "
        f"two arms each, {attempts} attempt(s) per task → {jobs_root}")

    rows: dict[str, dict] = {}
    for entry in agents:
        # "agent" or "agent@model": the question is which *combination* serves a skill best, so a
        # row is one harness paired with one model, not a harness alone.
        agent, _, combo_model = entry.partition("@")
        row_model = combo_model or model
        # One broken harness must not discard the arms already paid for.
        try:
            jobs_dir = jobs_root / entry.replace("/", "_")
            # The directory name has had its slashes flattened, so "openai/gpt-5.5" and a model
            # genuinely named "openai_gpt-5.5" are indistinguishable once written. Rescoring reads
            # only these directories, and a co-occurrence grid that cannot say which model a row
            # used is not a co-occurrence grid. Record the pair beside the arms.
            jobs_dir.mkdir(parents=True, exist_ok=True)
            (jobs_dir / "combo.json").write_text(json.dumps(
                {"combination": entry, "harness": agent, "model": row_model or "harness default"}))
            jobs, answers = {}, {}
            for arm in ("skill", "control"):
                jobs[arm] = run_arm(dataset, agent, source if arm == "skill" else None,
                                    jobs_dir, arm, row_model, concurrency, attempts, log)
                answers[arm] = collect_answers(jobs[arm], broken_trials(jobs[arm]))
            # A task that crashed in either arm is dropped from both, so the two means are always
            # over the same tasks. Otherwise the difference between them is not lift.
            skipped = broken_tasks(jobs["skill"]) | broken_tasks(jobs["control"])
            arms = {arm: score(answers[arm], skill, holdout, skipped) for arm in jobs}
            s_mean = sum(arms["skill"]) / len(arms["skill"])
            c_mean = sum(arms["control"]) / len(arms["control"])
            verdict = ("helps" if s_mean - c_mean > 0.05
                       else "no lift" if s_mean - c_mean >= -0.05 else "HURTS")
            note = f"  [{len(skipped)} task(s) dropped]" if skipped else ""
            log(f"[harbor] {entry:<38} skill {s_mean:.3f}  control {c_mean:.3f}  "
                f"lift {s_mean - c_mean:+.3f}  ({verdict}){note}")
            rows[entry] = {"skill_mean": s_mean, "control_mean": c_mean, "lift": s_mean - c_mean,
                           "skill_scores": arms["skill"], "control_scores": arms["control"],
                           "harness": agent, "model": row_model or "harness default",
                           # Not cosmetic: a lift over 2 tasks and one over 4 are different claims,
                           # and a reader comparing rows has to be able to see which is which.
                           "tasks_scored": len(arms["skill"]), "tasks_dropped": sorted(skipped),
                           "attempts": attempts}
        except Exception as error:  # noqa: BLE001 - any harness failure is one unusable row
            rows[entry] = {"error": f"{type(error).__name__}: {error}"[:400], "harness": agent,
                           "model": row_model or "harness default"}
            # The message, not just the type: when every row fails there is no matrix to read the
            # detail out of, and a log saying only "RuntimeError" cannot be diagnosed at all.
            log(f"[harbor] {entry:<38} UNAVAILABLE: {str(error)[:300]}")
    if not any("error" not in row for row in rows.values()):
        raise SystemExit(f"[harbor] no harness could be run for '{skill}'; nothing was measured.")

    summary = {"skill": skill, "tasks": len(holdout), "pinned_model": model,
               "judge": os.environ.get("JUDGE_MODELS") or os.environ.get("JUDGE_MODEL", ""),
               "harnesses": rows}
    HARBOR_DIR.mkdir(parents=True, exist_ok=True)
    path = HARBOR_DIR / f"{skill}.json"
    path.write_text(json.dumps(summary, indent=2))
    log(f"[harbor] matrix written to {path}")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sandboxed cross-harness skill evaluation.")
    parser.add_argument("skill")
    parser.add_argument("--agent", action="append", default=None,
                        help="harness to run (repeatable); default claude-code")
    parser.add_argument("--model", default=None, help="pin the model where the harness allows it")
    parser.add_argument("--target", action="append", default=None, metavar="ALIAS=URL",
                        help="local allowlisted endpoint (repeatable); enables local model sweep")
    parser.add_argument("-n", "--concurrent", type=int, default=2)
    parser.add_argument("--global-concurrency", type=int,
                        help="native Harbor global trial cap (default max(16, --concurrent))")
    parser.add_argument("--endpoint-concurrency", type=int,
                        help="native Harbor per-endpoint cap (default --concurrent)")
    parser.add_argument("-k", "--attempts", type=int, default=None,
                        help="attempts per task per arm, averaged; 1 is below this eval's noise")
    parser.add_argument("--canary-only", action="store_true",
                        help="run endpoint preflights and one-task routing canaries without full arms")
    parser.add_argument("--exploratory", action="store_true",
                        help="allow a one-attempt local sweep that is explicitly not rankable")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.canary_only and not args.target:
        parser.error("--canary-only requires --target")
    if args.exploratory and not args.target:
        parser.error("--exploratory requires --target")
    if args.exploratory and args.attempts != 1:
        parser.error("--exploratory requires --attempts 1")
    if args.target:
        if args.model is not None:
            parser.error("--target cannot be combined with --model; targets pin their served model")
        attempts = 3 if args.attempts is None else args.attempts
        if attempts == 1 and not args.exploratory:
            parser.error("--attempts 1 requires --exploratory")
        if attempts not in (1, 3):
            parser.error("--target requires --attempts 3, or 1 with --exploratory")
        targets = []
        for spec in args.target:
            provisional = parse_target(spec)
            # parse_target enforces the allowlist and canonical URL before discovery makes a request.
            targets.append(discover_target(provisional.alias, provisional.base_url))
        manifest = run_local_sweep(args.skill, targets,
                                   harnesses=args.agent or list(LOCAL_HARNESSES),
                                   concurrency=args.concurrent, attempts=attempts,
                                   global_concurrency=args.global_concurrency,
                                   endpoint_concurrency=args.endpoint_concurrency,
                                   canary_only=args.canary_only,
                                   exploratory=args.exploratory, native_parallel=True, log=print)
        return 1 if manifest.get("aborted") else 0
    run_harbor_eval(args.skill, args.agent or ["claude-code"], model=args.model,
                    concurrency=args.concurrent, attempts=args.attempts or 1, log=print)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
