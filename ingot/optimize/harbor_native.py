"""Compile and recover exact Ingot identities in native Harbor jobs."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .harbor_gateway import (gateway_agent_env, gateway_agent_name, gateway_route)
from .harbor_targets import (HARNESS_PROTOCOLS, LocalTarget, harbor_agent_kwargs,
                             harbor_model, local_agent_env, protocol_for)

NATIVE_TRIAL_MEMORY_MB = 2_048
NATIVE_RUNNER_REVISION = f"native-v4-memory{NATIVE_TRIAL_MEMORY_MB}mb"

_IDENTITY_KEYS = {
    "INGOT_COMBINATION_ID": "combination_id",
    "INGOT_ENDPOINT_FINGERPRINT": "endpoint_fingerprint",
    "INGOT_HARNESS": "harness",
    "INGOT_PROTOCOL": "protocol",
    "INGOT_GATEWAY_REVISION": "gateway_revision",
    "INGOT_ARM": "arm",
}
_FINGERPRINT = re.compile(r"^[0-9a-f]{12}$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class NativeTrialIdentity:
    combination_id: str
    endpoint_fingerprint: str
    harness: str
    protocol: str
    gateway_revision: str
    arm: str

    def __post_init__(self) -> None:
        values = tuple(self.__dict__.values())
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("Harbor trial identity fields must be nonempty strings")
        if any(token in self.combination_id for token in ("/", "\\", "\x00", "..")):
            raise ValueError("Harbor trial identity combination is not path-safe")
        if not _FINGERPRINT.fullmatch(self.endpoint_fingerprint):
            raise ValueError("Harbor trial identity endpoint fingerprint is invalid")
        if self.harness not in HARNESS_PROTOCOLS:
            raise ValueError("Harbor trial identity harness is unsupported")
        if self.protocol != protocol_for(self.harness):
            raise ValueError("Harbor trial identity protocol does not match its harness")
        if not _REVISION.fullmatch(self.gateway_revision):
            raise ValueError("Harbor trial identity gateway revision is invalid")
        if self.arm not in {"canary", "skill", "control"}:
            raise ValueError("Harbor trial identity arm is invalid")
        if not self.combination_id.startswith(f"{self.harness}@"):
            raise ValueError("Harbor trial identity combination does not match its harness")
        if self.endpoint_fingerprint not in self.combination_id:
            raise ValueError("Harbor trial identity combination does not match its endpoint")


@dataclass(frozen=True)
class NativeCell:
    target: LocalTarget
    harness: str

    @property
    def combination_id(self) -> str:
        return native_trial_identity(self.target, self.harness, "skill").combination_id


def identity_env(identity: NativeTrialIdentity) -> dict[str, str]:
    """Return the exact non-secret fields Harbor persists on ``agent.env``."""
    return {key: getattr(identity, field) for key, field in _IDENTITY_KEYS.items()}


def identity_from_env(env: Mapping[str, object]) -> NativeTrialIdentity:
    """Parse exactly the six persisted Ingot identity fields from an agent environment."""
    ingot_keys = {key for key in env if isinstance(key, str) and key.startswith("INGOT_")}
    if ingot_keys != set(_IDENTITY_KEYS):
        raise ValueError("Harbor trial identity fields are incomplete or ambiguous")
    values = {field: env[key] for key, field in _IDENTITY_KEYS.items()}
    try:
        return NativeTrialIdentity(**values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Harbor trial identity is invalid: {exc}") from exc


def read_trial_identity(trial_dir: Path) -> NativeTrialIdentity:
    """Recover identity from Harbor's resolved per-trial lock and fail closed."""
    lock_path = Path(trial_dir) / "lock.json"
    try:
        lock_info = lock_path.lstat()
        if not stat.S_ISREG(lock_info.st_mode):
            raise ValueError("Harbor trial identity lock is not a regular file")
        payload = json.loads(lock_path.read_text())
        env = payload["agent"]["env"]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("Harbor trial identity lock is unreadable") from exc
    if not isinstance(env, dict):
        raise ValueError("Harbor trial identity environment is invalid")
    return identity_from_env(env)


def iter_attempt_dirs(job_dir: Path, *, identity: NativeTrialIdentity | None = None
                      ) -> Iterator[Path]:
    """Yield direct Harbor attempt directories, optionally by exact persisted identity."""
    job_dir = Path(job_dir)
    try:
        job_info = job_dir.lstat()
    except OSError as exc:
        raise ValueError("native Harbor job is not a real directory") from exc
    if stat.S_ISLNK(job_info.st_mode) or not stat.S_ISDIR(job_info.st_mode):
        raise ValueError("native Harbor job is not a real directory")
    for attempt in sorted(job_dir.iterdir()):
        info = attempt.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("native Harbor entry is not a real attempt directory")
        if not stat.S_ISDIR(info.st_mode):
            continue
        result = attempt / "result.json"
        try:
            result_info = result.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(result_info.st_mode):
            raise ValueError("native Harbor result is not a regular file")
        if identity is None:
            yield attempt
            continue
        try:
            lock_info = (attempt / "lock.json").lstat()
        except OSError:
            continue
        if not stat.S_ISREG(lock_info.st_mode):
            raise ValueError("native Harbor lock is not a regular file")
        try:
            observed = read_trial_identity(attempt)
        except ValueError:
            continue
        if observed == identity:
            yield attempt


def native_trial_identity(target: LocalTarget, harness: str, arm: str) -> NativeTrialIdentity:
    """Derive the only valid persisted identity for one target, harness, and arm."""
    route = gateway_route(target, harness)
    return NativeTrialIdentity(
        combination_id=f"{harness}@{target.served_model}--{target.job_slug}",
        endpoint_fingerprint=target.fingerprint,
        harness=harness,
        protocol=protocol_for(harness),
        gateway_revision=f"{route.revision if route else 'direct'}-{NATIVE_RUNNER_REVISION}",
        arm=arm,
    )


def compile_agent_config(target: LocalTarget, harness: str, arm: str, skill_source: Path,
                         endpoint_limit: int) -> dict:
    """Compile one Harbor agent entry whose resolved lock retains exact cell identity."""
    if not isinstance(endpoint_limit, int) or isinstance(endpoint_limit, bool) or endpoint_limit < 1:
        raise ValueError("endpoint concurrency must be a positive integer")

    identity = native_trial_identity(target, harness, arm)
    route = gateway_route(target, harness)
    agent = gateway_agent_name(route) if route else harness
    env = gateway_agent_env(target, route) if route else local_agent_env(target, harness)
    config = {
        "model_name": route.model if route else harbor_model(target, harness),
        "n_concurrent": endpoint_limit,
        "concurrency_group": f"endpoint:{target.fingerprint}",
        "skills": [str(skill_source)] if arm in {"skill", "canary"} else [],
        "kwargs": {} if route else harbor_agent_kwargs(target, harness),
        "env": {**env, **identity_env(identity)},
    }
    if ":" in agent:
        config["import_path"] = agent
    else:
        config["name"] = agent
    return config


def _job_base(dataset: Path, task_names: Sequence[str], jobs_dir: Path, attempts: int,
              global_limit: int, agents: list[dict]) -> dict:
    if (not isinstance(global_limit, int) or isinstance(global_limit, bool)
            or global_limit < 1):
        raise ValueError("global concurrency must be a positive integer")
    names = list(task_names)
    if not names or not all(isinstance(name, str) and name for name in names):
        raise ValueError("task names must be nonempty strings")
    if len(names) != len(set(names)):
        raise ValueError("task names must be unique")
    return {
        "jobs_dir": str(jobs_dir),
        "n_attempts": attempts,
        "n_concurrent_trials": global_limit,
        "agents": agents,
        "datasets": [{"path": str(dataset), "task_names": names}],
    }


def _limits(cells: Sequence[NativeCell], endpoint_limits: Mapping[str, int]) -> None:
    required = {cell.target.fingerprint for cell in cells}
    if set(endpoint_limits) != required:
        raise ValueError("endpoint concurrency must cover exactly the requested targets")
    for value in endpoint_limits.values():
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError("endpoint concurrency must be a positive integer")
    identities = [cell.combination_id for cell in cells]
    if len(identities) != len(set(identities)):
        raise ValueError("native Harbor cells must have unique combination identities")


def compile_canary_job(dataset: Path, task_name: str, cells: Sequence[NativeCell],
                       skill_source: Path, jobs_dir: Path, *, global_limit: int,
                       endpoint_limits: Mapping[str, int]) -> dict:
    """Compile one skill-bearing attempt for every requested model/harness cell."""
    cells = list(cells)
    if not cells:
        raise ValueError("native Harbor job requires at least one cell")
    _limits(cells, endpoint_limits)
    agents = [compile_agent_config(cell.target, cell.harness, "canary", skill_source,
                                   endpoint_limits[cell.target.fingerprint])
              for cell in cells]
    return _job_base(dataset, [task_name], jobs_dir, 1, global_limit, agents)


def compile_measurement_job(dataset: Path, task_names: Sequence[str], cells: Sequence[NativeCell],
                            skill_source: Path, jobs_dir: Path, *, attempts: int,
                            global_limit: int,
                            endpoint_limits: Mapping[str, int],
                            arms: Sequence[str] = ("skill", "control")) -> dict:
    """Compile the requested exact arms for every canary-approved cell."""
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts != 3:
        raise ValueError("full measurement requires exactly three attempts")
    names = list(task_names)
    if len(names) != 4:
        raise ValueError("full measurement requires exactly four tasks")
    cells = list(cells)
    if not cells:
        raise ValueError("native Harbor job requires at least one cell")
    arms = tuple(arms)
    if not arms or len(arms) != len(set(arms)) or not set(arms) <= {"skill", "control"}:
        raise ValueError("measurement arms must be a unique nonempty skill/control subset")
    _limits(cells, endpoint_limits)
    agents = []
    buckets: dict[str, list[NativeCell]] = {}
    for cell in cells:
        buckets.setdefault(cell.target.fingerprint, []).append(cell)
    ordered = []
    while any(buckets.values()):
        for fingerprint in buckets:
            if buckets[fingerprint]:
                ordered.append(buckets[fingerprint].pop(0))
    for cell in ordered:
        limit = endpoint_limits[cell.target.fingerprint]
        for arm in arms:
            agents.append(compile_agent_config(cell.target, cell.harness, arm, skill_source, limit))
    return _job_base(dataset, names, jobs_dir, attempts, global_limit, agents)


def select_measurement_cells(cells: Sequence[NativeCell], canaries: Mapping[str, Mapping[str, object]]
                             ) -> tuple[list[NativeCell], dict[str, dict[str, object]]]:
    """Partition exact passed canaries from explicit unmeasured cell records."""
    selected = []
    unmeasured = {}
    for cell in cells:
        record = canaries.get(cell.combination_id)
        if isinstance(record, Mapping) and record.get("ok") is True and "error" not in record:
            selected.append(cell)
            continue
        error = str((record or {}).get("error") or "canary did not pass")[:400]
        unmeasured[cell.combination_id] = {
            "combination": cell.combination_id,
            "harness": cell.harness,
            "target_alias": cell.target.alias,
            "endpoint_fingerprint": cell.target.fingerprint,
            "state": "unmeasured",
            "error": error,
        }
    return selected, unmeasured


def write_job_config(path: Path, config: Mapping[str, object]) -> None:
    """Write one deterministic Harbor job document without exposing a partial file."""
    path = Path(path)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", path.name):
        raise ValueError("Harbor config filename must be a safe slug")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", suffix=".tmp",
                                     delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(config, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
