"""Export persisted Harbor attempts to Langfuse with public read-back receipts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx

from .harbor_redaction import _redact_harbor_receipt_output, _redact_persisted
from .harbor_native import NativeTrialIdentity, read_trial_identity


EXPORTER_REVISION = "harbor-langfuse-v3"
RECEIPT_NAME = "langfuse-receipt.json"
LEGACY_V2_RECEIPT_NAME = "langfuse-receipt.v2.json"
_METADATA_FIELDS = (
    "combination", "harness", "model", "target_alias", "endpoint_fingerprint", "protocol",
    "task_fingerprint", "attempts", "gateway_revision", "gateway_identity", "gateway_agent",
    "arm", "canary", "skill", "skill_sha256",
)
_MAX_TEXT_BYTES = 64 * 1024
# A retained Goose full-arm trajectory reached 3,368,055 bytes. Four MiB leaves measured headroom;
# the outbound projection still retains only allowlisted timestamps, sources, and numeric totals.
_MAX_TRAJECTORY_BYTES = 4 * 1024 * 1024
_MAX_ARTIFACT_PATHS = 128
_MAX_ARTIFACT_SCAN_PATHS = 4096
_MAX_ARTIFACT_DEPTH = 16
_MAX_ARTIFACT_BYTES = 128 * 1024
_READBACK_ATTEMPTS = 5
_READBACK_POLL_SECONDS = 1.0
_TEXT_SUFFIXES = {
    ".c", ".cc", ".cpp", ".css", ".csv", ".h", ".html", ".ini", ".java", ".js",
    ".json", ".jsx", ".log", ".md", ".py", ".rb", ".rs", ".rst", ".sh", ".sql",
    ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
}
_TEXT_NAMES = {"Dockerfile", "Makefile"}
_STRUCTURAL_EXCLUSIONS = {".env", "config", "credentials", "env", "environment", "secrets"}
_SENSITIVE_ARTIFACT_STEMS = {
    "config", "configuration", "credential", "credentials", "endpoint", "endpoints",
    "env", "environment", "secret", "secrets",
}


class TelemetryReceiptError(RuntimeError):
    """Persisted Harbor evidence lacks a matching, publicly readable trace receipt."""


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino, stat.S_IFMT(first.st_mode)) == (
        second.st_dev, second.st_ino, stat.S_IFMT(second.st_mode))


class _AttemptReader:
    """Read only the fixed Harbor attempt layout through job-relative descriptors."""

    def __init__(self, trial: Path) -> None:
        self.trial = trial
        self._job_fd = -1
        self._trial_fd = -1
        self._artifact_paths = 0
        self._artifact_files = 0
        self._artifact_bytes = 0
        self._artifact_omitted_files = 0
        self._artifact_omitted_hidden = 0
        self._artifact_scan_truncated = False

    def __enter__(self) -> "_AttemptReader":
        try:
            job_info = self.trial.parent.lstat()
            if stat.S_ISLNK(job_info.st_mode) or not stat.S_ISDIR(job_info.st_mode):
                raise TelemetryReceiptError(f"{self.trial.parent} is not a real job directory")
            self._job_fd = os.open(self.trial.parent, _DIRECTORY_FLAGS)
            if not _same_file(job_info, os.fstat(self._job_fd)):
                raise TelemetryReceiptError(f"{self.trial.parent} changed during admission")
            trial_info = os.stat(self.trial.name, dir_fd=self._job_fd, follow_symlinks=False)
            if stat.S_ISLNK(trial_info.st_mode) or not stat.S_ISDIR(trial_info.st_mode):
                raise TelemetryReceiptError(f"{self.trial} is a symlink or non-directory attempt")
            self._trial_fd = os.open(self.trial.name, _DIRECTORY_FLAGS, dir_fd=self._job_fd)
            if not _same_file(trial_info, os.fstat(self._trial_fd)):
                raise TelemetryReceiptError(f"{self.trial} changed during admission")
            return self
        except (OSError, TelemetryReceiptError):
            self.close()
            raise

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        for descriptor in (self._trial_fd, self._job_fd):
            if descriptor >= 0:
                os.close(descriptor)
        self._trial_fd = self._job_fd = -1

    def _open_dir(self, parent_fd: int, name: str, *, missing_ok: bool = False) -> tuple[int, os.stat_result] | None:
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise TelemetryReceiptError(f"attempt directory {name!r} failed admission")
        try:
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except OSError as error:
            raise TelemetryReceiptError(f"attempt directory {name!r} failed admission") from error
        if not _same_file(before, os.fstat(descriptor)):
            os.close(descriptor)
            raise TelemetryReceiptError(f"attempt directory {name!r} changed during admission")
        return descriptor, before

    @staticmethod
    def _verify_entry(parent_fd: int, name: str, opened: os.stat_result) -> None:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise TelemetryReceiptError(f"attempt entry {name!r} changed during admission") from error
        if not _same_file(opened, current):
            raise TelemetryReceiptError(f"attempt entry {name!r} changed during admission")

    def _read_at(self, parent_fd: int, name: str, *, artifact: bool = False,
                 limit: int = _MAX_TEXT_BYTES) -> bytes | None:
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise TelemetryReceiptError(f"attempt entry {name!r} is non-regular")
        if before.st_size > limit:
            raise TelemetryReceiptError(f"attempt artifact byte budget exceeded by {name!r}")
        try:
            descriptor = os.open(
                name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        except OSError as error:
            raise TelemetryReceiptError(f"attempt entry {name!r} failed admission") from error
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or not _same_file(before, opened):
                raise TelemetryReceiptError(f"attempt entry {name!r} changed during admission")
            chunks = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(descriptor)
        self._verify_entry(parent_fd, name, before)
        if len(data) > limit:
            raise TelemetryReceiptError(f"attempt artifact byte budget exceeded by {name!r}")
        return data

    def read(self, *parts: str, limit: int = _MAX_TEXT_BYTES) -> bytes | None:
        descriptor = os.dup(self._trial_fd)
        opened_dirs: list[tuple[int, str, os.stat_result]] = []
        try:
            for part in parts[:-1]:
                opened = self._open_dir(descriptor, part, missing_ok=True)
                if opened is None:
                    return None
                child_fd, child_info = opened
                opened_dirs.append((descriptor, part, child_info))
                descriptor = child_fd
            return self._read_at(descriptor, parts[-1], limit=limit)
        finally:
            os.close(descriptor)
            for parent_fd, name, opened in reversed(opened_dirs):
                self._verify_entry(parent_fd, name, opened)
                os.close(parent_fd)

    def read_json(self, *parts: str, limit: int = _MAX_TEXT_BYTES) -> dict:
        data = self.read(*parts, limit=limit)
        if data is None:
            return {}
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def artifacts(self, *parts: str, skip_solution: bool = False,
                  skill_body: bytes | None = None,
                  first: Sequence[str] = ()) -> dict[str, str]:
        descriptor = os.dup(self._trial_fd)
        opened_dirs: list[tuple[int, str, os.stat_result]] = []
        try:
            for part in parts:
                opened = self._open_dir(descriptor, part, missing_ok=True)
                if opened is None:
                    return {}
                child_fd, child_info = opened
                opened_dirs.append((descriptor, part, child_info))
                descriptor = child_fd
            found: list[tuple[str, str]] = []
            for name in first:
                try:
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                self._project_artifact(descriptor, name, Path(name), info, found, skill_body)
            self._walk_artifacts(descriptor, (), found, skip_solution, skill_body, depth=0,
                                 excluded=frozenset(first))
            return dict(sorted(found))
        finally:
            os.close(descriptor)
            for parent_fd, name, opened in reversed(opened_dirs):
                self._verify_entry(parent_fd, name, opened)
                os.close(parent_fd)

    def artifact_projection(self) -> dict[str, int | bool]:
        """Describe a bounded omission without turning optional telemetry into failed evidence."""
        return {
            "exported_files": self._artifact_files,
            "exported_bytes": self._artifact_bytes,
            "omitted_files": self._artifact_omitted_files,
            "omitted_hidden_paths": self._artifact_omitted_hidden,
            "scan_truncated": self._artifact_scan_truncated,
        }

    def _project_artifact(self, descriptor: int, name: str, path: Path,
                          info: os.stat_result, found: list[tuple[str, str]],
                          skill_body: bytes | None) -> None:
        if stat.S_ISLNK(info.st_mode):
            raise TelemetryReceiptError(f"attempt artifact {name!r} failed admission")
        if not stat.S_ISREG(info.st_mode):
            raise TelemetryReceiptError(f"attempt artifact {name!r} is non-regular")
        if name == RECEIPT_NAME:
            return
        lowered = {part.lower() for part in path.parts}
        if (lowered & _STRUCTURAL_EXCLUSIONS
                or path.stem.lower() in _SENSITIVE_ARTIFACT_STEMS):
            return
        if path.suffix.lower() not in _TEXT_SUFFIXES and name not in _TEXT_NAMES:
            return
        if (info.st_size > _MAX_TEXT_BYTES
                or self._artifact_files >= _MAX_ARTIFACT_PATHS
                or info.st_size > _MAX_ARTIFACT_BYTES - self._artifact_bytes):
            self._artifact_omitted_files += 1
            return
        data = self._read_at(descriptor, name)
        assert data is not None
        if skill_body and skill_body in data:
            self._artifact_omitted_files += 1
            return
        if any(byte < 32 and byte not in (9, 10, 13) or byte == 127 for byte in data):
            self._artifact_omitted_files += 1
            return
        try:
            value = data.decode("utf-8")
        except UnicodeDecodeError:
            self._artifact_omitted_files += 1
            return
        found.append((path.as_posix(), _redact_harbor_receipt_output(value, {})))
        self._artifact_files += 1
        self._artifact_bytes += len(data)

    def _walk_artifacts(self, descriptor: int, relative: tuple[str, ...],
                        found: list[tuple[str, str]], skip_solution: bool,
                        skill_body: bytes | None, *, depth: int,
                        excluded: frozenset[str] = frozenset()) -> None:
        if self._artifact_scan_truncated:
            return
        if depth > _MAX_ARTIFACT_DEPTH:
            self._artifact_omitted_files += 1
            return
        names = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if self._artifact_paths >= _MAX_ARTIFACT_SCAN_PATHS:
                    self._artifact_scan_truncated = True
                    break
                self._artifact_paths += 1
                # Harbor and harnesses may retain caches/backups beside the submitted solution.
                # They are never evidence. Skip hidden entries at every depth before stat or
                # traversal, so even a hidden symlink cannot redirect the exporter.
                if entry.name.startswith("."):
                    self._artifact_omitted_hidden += 1
                    continue
                names.append(entry.name)
        for name in sorted(names, key=lambda item: (item != "answer.md", item)):
            if self._artifact_scan_truncated:
                return
            if depth == 0 and name in excluded:
                continue
            path_parts = (*relative, name)
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise TelemetryReceiptError(f"attempt artifact {name!r} failed admission")
            if stat.S_ISDIR(info.st_mode):
                if skip_solution and path_parts == ("solution",):
                    continue
                opened = self._open_dir(descriptor, name)
                assert opened is not None
                child_fd, child_info = opened
                try:
                    self._walk_artifacts(child_fd, path_parts, found, skip_solution, skill_body,
                                         depth=depth + 1)
                finally:
                    os.close(child_fd)
                self._verify_entry(descriptor, name, child_info)
                continue
            self._project_artifact(descriptor, name, Path(*path_parts), info, found, skill_body)


def _path_json(path: Path, limit: int = _MAX_TEXT_BYTES) -> dict:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return {}
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            data = os.read(descriptor, limit + 1)
        finally:
            os.close(descriptor)
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def load_provenance_metadata(path: Path) -> dict:
    """Load one explicit bounded migration document used at export and publication."""
    metadata = _path_json(path)
    if not metadata:
        raise TelemetryReceiptError(f"{path} has no readable provenance metadata")
    return metadata


def _clean_string(value: object) -> str:
    # Canonical payload identity cannot depend on which credentials happen to be active now.
    return _redact_harbor_receipt_output(value, {})


def _metadata(metadata: Mapping[str, object]) -> dict:
    clean = {}
    for key in _METADATA_FIELDS:
        value = metadata.get(key)
        if isinstance(value, str):
            clean[key] = _clean_string(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            if key in metadata:
                clean[key] = value
    return clean


def _trajectory(reader: _AttemptReader) -> dict:
    record = reader.read_json(
        "agent", "trajectory.json", limit=_MAX_TRAJECTORY_BYTES)
    if not record:
        return {}
    result = {"steps": []}
    raw_steps = record.get("steps", [])
    if not isinstance(raw_steps, list):
        raise TelemetryReceiptError("trajectory steps must be a list")
    for raw in raw_steps:
        if not isinstance(raw, Mapping):
            continue
        step = {}
        if raw.get("source") in ("user", "agent"):
            step["source"] = raw["source"]
        timestamp = raw.get("timestamp")
        if isinstance(timestamp, str) and re.fullmatch(
                r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z", timestamp):
            step["timestamp"] = timestamp
        result["steps"].append(step)
    if isinstance(record.get("final_metrics"), Mapping):
        final_metrics = record["final_metrics"]
        result["final_metrics"] = {}
        for key in (
                "total_prompt_tokens", "total_cached_tokens",
                "total_completion_tokens", "total_cost_usd",
        ):
            value = final_metrics.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result["final_metrics"][key] = value
    return result


def _exception_evidence(reader: _AttemptReader,
                        result: Mapping[str, object]) -> tuple[str | None, str | None]:
    info = result.get("exception_info")
    if isinstance(info, Mapping) and info.get("exception_type"):
        detail = _clean_string(info.get("exception_message")) or None
        return _clean_string(info["exception_type"]), detail
    data = reader.read("exception.txt")
    if data:
        try:
            exception = _redact_harbor_receipt_output(data.decode("utf-8"), {})
        except UnicodeDecodeError:
            return None, None
        matches = re.findall(r"(?:^|\n)([A-Za-z_][A-Za-z0-9_.]*)(?=:\s)", exception)
        detail = exception.rsplit("\n", 1)[-1]
        return (matches[-1] if matches else "Exception"), detail
    return None, None


def _task_slug(task_name: object) -> str:
    return str(task_name or "").split("/")[-1].split("__", 1)[0]


def _task_text(metadata: Mapping[str, object], task_name: object) -> str:
    texts = metadata.get("task_texts")
    if not isinstance(texts, Mapping):
        return ""
    return _clean_string(texts.get(_task_slug(task_name)))


def _timestamps(result: Mapping[str, object], trajectory: Mapping[str, object]) -> dict[str, str]:
    timestamps = {}
    for source, destination in (("started_at", "started_at"), ("finished_at", "finished_at")):
        value = result.get(source)
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z", value):
            timestamps[destination] = value
    info = result.get("exception_info")
    occurred_at = info.get("occurred_at") if isinstance(info, Mapping) else None
    if (isinstance(occurred_at, str)
            and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z", occurred_at)):
        timestamps["error_at"] = occurred_at
    step_times = [step.get("timestamp") for step in trajectory.get("steps") or []
                  if isinstance(step, Mapping) and isinstance(step.get("timestamp"), str)]
    if step_times:
        timestamps.setdefault("started_at", step_times[0])
        timestamps.setdefault("finished_at", step_times[-1])
    return timestamps


def _known_skill_body(metadata: Mapping[str, object]) -> bytes | None:
    body = metadata.get("skill_body")
    digest = metadata.get("skill_sha256")
    if body in (None, "") and digest in (None, ""):
        return None
    if not isinstance(body, str) or not isinstance(digest, str):
        raise TelemetryReceiptError("skill provenance is incomplete")
    encoded = body.encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise TelemetryReceiptError("skill provenance SHA-256 does not match its body")
    return encoded


def _usage(result: Mapping[str, object], trajectory: Mapping[str, object]) -> dict:
    agent_result = result.get("agent_result")
    agent_result = agent_result if isinstance(agent_result, Mapping) else {}
    fields = {
        "input_tokens": agent_result.get("n_input_tokens"),
        "cache_tokens": agent_result.get("n_cache_tokens"),
        "output_tokens": agent_result.get("n_output_tokens"),
        "cost_usd": agent_result.get("cost_usd"),
    }
    final = trajectory.get("final_metrics")
    if isinstance(final, Mapping):
        fields = {
            "input_tokens": fields["input_tokens"] or final.get("total_prompt_tokens"),
            "cache_tokens": fields["cache_tokens"] or final.get("total_cached_tokens"),
            "output_tokens": fields["output_tokens"] or final.get("total_completion_tokens"),
            "cost_usd": fields["cost_usd"] or final.get("total_cost_usd"),
        }
    return {key: value for key, value in fields.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)}


def build_attempt_payload(trial: Path, metadata: Mapping[str, object]) -> dict:
    """Return the bounded, normalized telemetry payload for one persisted Harbor trial."""
    with _AttemptReader(trial) as reader:
        result = reader.read_json("result.json")
        trajectory = _trajectory(reader)
        exception_category, error_detail = _exception_evidence(reader, result)
        skill_body = _known_skill_body(metadata)
        solution_artifacts = reader.artifacts(
            "verifier", "solution", skill_body=skill_body, first=("answer.md",))
        verifier_output = reader.artifacts(
            "verifier", skip_solution=True, skill_body=skill_body)
        artifact_projection = reader.artifact_projection()
    agent_info = result.get("agent_info")
    agent_info = agent_info if isinstance(agent_info, Mapping) else {}
    model_info = agent_info.get("model_info")
    model_info = model_info if isinstance(model_info, Mapping) else {}
    model = metadata.get("model") or model_info.get("name")
    task_name = result.get("task_name") or trial.name.split("__", 1)[0]
    terminal_success = (bool(result)
                        and isinstance(result.get("finished_at"), str)
                        and bool(result["finished_at"].strip())
                        and result.get("exception_info") is None)
    payload = {
        "exporter_revision": EXPORTER_REVISION,
        "attempt": {
            "id": _clean_string(result.get("id") or trial.name),
            "trial_name": _clean_string(result.get("trial_name") or trial.name),
        },
        "task": {
            "name": _clean_string(task_name),
            "checksum": _clean_string(result.get("task_checksum")),
            "source": _clean_string(result.get("source")),
            "text": _task_text(metadata, task_name),
        },
        "skill": _clean_string(metadata.get("skill")),
        "trajectory": trajectory,
        "verifier_output": verifier_output,
        "solution_artifacts": solution_artifacts,
        "status": "failed" if exception_category else "succeeded" if terminal_success else "incomplete",
        "exception_category": exception_category,
        "error_detail": error_detail,
        "timestamps": _timestamps(result, trajectory),
        "usage": _usage(result, trajectory),
        "model": _clean_string(model),
        "metadata": _metadata(metadata),
    }
    if (artifact_projection["omitted_files"] or artifact_projection["omitted_hidden_paths"]
            or artifact_projection["scan_truncated"]):
        payload["artifact_projection"] = artifact_projection
    return payload


def _payload_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_receipt(path: Path, receipt: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _expected_trace_id(digest: str) -> str:
    from langfuse import Langfuse
    return Langfuse.create_trace_id(seed=f"{EXPORTER_REVISION}:{digest}")


def _expected_receipt(digest: str) -> dict:
    return {
        "status": "verified",
        "trace_id": _expected_trace_id(digest),
        "payload_sha256": digest,
        "exporter_revision": EXPORTER_REVISION,
    }


def _pending_receipt(digest: str, observation_id: str | None = None) -> dict:
    receipt = {
        "status": "pending",
        "trace_id": _expected_trace_id(digest),
        "payload_sha256": digest,
        "exporter_revision": EXPORTER_REVISION,
    }
    if observation_id:
        receipt["observation_id"] = observation_id
    return receipt


def _read_receipt(trial: Path) -> dict:
    with _AttemptReader(trial) as reader:
        return reader.read_json(RECEIPT_NAME)


def _verified_receipt(trial: Path, digest: str) -> dict | None:
    receipt = _read_receipt(trial)
    if receipt == _expected_receipt(digest):
        return receipt
    return None


def _create_pending_receipt(path: Path, receipt: Mapping[str, object]) -> bool:
    encoded = json.dumps(receipt, separators=(",", ":")).encode("utf-8")
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        return False
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def _read_back(trace_id: str, client) -> dict | None:
    if hasattr(client, "read_trace"):
        value = client.read_trace(trace_id)
        return value if isinstance(value, dict) else None
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    base_url = os.environ.get("LANGFUSE_BASE_URL", "http://langfuse-web:3000").rstrip("/")
    if not public_key or not secret_key:
        raise TelemetryReceiptError("Langfuse public read-back credentials are missing")
    response = httpx.get(f"{base_url}/api/public/traces/{trace_id}",
                         auth=(public_key, secret_key), timeout=15)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise TelemetryReceiptError(
            f"Langfuse public read-back returned status {response.status_code}")
    value = response.json()
    return value if isinstance(value, dict) else None


def _telemetry_evidence(payload: Mapping[str, object]) -> dict:
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
    return {
        "revision": "harbor-agent-evidence-v1",
        "model": payload.get("model") or "",
        "status": payload.get("status") or "incomplete",
        "tokens": {key: usage[key] for key in (
            "input_tokens", "cache_tokens", "output_tokens") if key in usage},
    }


def _matching_observation_id(value: dict | None, trace_id: str, digest: str,
                             evidence: Mapping[str, object],
                             observation_id: str | None = None, *,
                             exporter_revision: str = EXPORTER_REVISION) -> str | None:
    if not value or value.get("id") != trace_id:
        return None
    observations = value.get("observations")
    if not isinstance(observations, list) or len(observations) != 1:
        return None
    observation = observations[0]
    if not isinstance(observation, Mapping):
        return None
    metadata = observation.get("metadata")
    actual_id = observation.get("id")
    if (not isinstance(actual_id, str) or not actual_id.strip()
            or (observation_id is not None and actual_id != observation_id)
            or observation.get("name") != "harbor-attempt"
            or str(observation.get("type") or "").lower() != "agent"
            or not isinstance(metadata, Mapping)
            or metadata.get("payload_sha256") != digest
            or metadata.get("exporter_revision") != exporter_revision
            or metadata.get("telemetry_evidence") != evidence):
        return None
    return actual_id


def _wait_for_matching_observation(trace_id: str, digest: str,
                                   evidence: Mapping[str, object],
                                   observation_id: str, client) -> bool:
    for attempt in range(_READBACK_ATTEMPTS):
        if _matching_observation_id(
                _read_back(trace_id, client), trace_id, digest, evidence, observation_id):
            return True
        if attempt + 1 < _READBACK_ATTEMPTS:
            time.sleep(_READBACK_POLL_SECONDS)
    return False


def _client():
    from langfuse import Langfuse
    return Langfuse()


def _resume_pending(trial: Path, current: Mapping[str, object], pending: dict,
                    trace_id: str, digest: str, evidence: Mapping[str, object], client) -> dict:
    if current == pending:
        raise TelemetryReceiptError(
            f"Langfuse trace {trace_id} has unknown pending observation state")
    observation_id = current.get("observation_id")
    if not (current.get("status") == "pending"
            and current.get("trace_id") == trace_id
            and current.get("payload_sha256") == digest
            and current.get("exporter_revision") == EXPORTER_REVISION
            and isinstance(observation_id, str)
            and bool(observation_id.strip())):
        raise TelemetryReceiptError(f"{trial} has a stale or malformed Langfuse receipt")
    if _wait_for_matching_observation(trace_id, digest, evidence, observation_id, client):
        receipt = _expected_receipt(digest)
        _atomic_receipt(trial / RECEIPT_NAME, receipt)
        return receipt
    raise TelemetryReceiptError(
        f"Langfuse trace {trace_id} remains pending public read-back")


def _migrate_verified_v2_receipt(trial: Path, current: Mapping[str, object], pending: dict,
                                 evidence: Mapping[str, object], client) -> bool:
    """Preserve one exact verified v2 receipt before authorizing telemetry-only v3 export."""
    from langfuse import Langfuse

    digest = current.get("payload_sha256")
    trace_id = current.get("trace_id")
    if not (current.get("status") == "verified"
            and current.get("exporter_revision") == "harbor-langfuse-v2"
            and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
            and isinstance(trace_id, str)
            and trace_id == Langfuse.create_trace_id(seed=f"harbor-langfuse-v2:{digest}")):
        return False
    if not _matching_observation_id(
            _read_back(trace_id, client), trace_id, digest, evidence,
            exporter_revision="harbor-langfuse-v2"):
        raise TelemetryReceiptError(f"{trial} has no matching verified v2 Langfuse trace")
    archive = trial / LEGACY_V2_RECEIPT_NAME
    if not _create_pending_receipt(archive, current):
        if _path_json(archive) != dict(current):
            raise TelemetryReceiptError(f"{trial} has a conflicting archived v2 receipt")
    _atomic_receipt(trial / RECEIPT_NAME, pending)
    return True


def _export_attempt(trial: Path, metadata: Mapping[str, object], client=None) -> dict:
    payload = build_attempt_payload(trial, metadata)
    digest = _payload_sha256(payload)
    evidence = _telemetry_evidence(payload)
    if receipt := _verified_receipt(trial, digest):
        return receipt
    client = client or _client()
    trace_id = client.create_trace_id(seed=f"{EXPORTER_REVISION}:{digest}")
    if trace_id != _expected_trace_id(digest):
        raise TelemetryReceiptError("Langfuse returned a non-deterministic trace ID")
    pending = _pending_receipt(digest)
    current = _read_receipt(trial)
    migrated = False
    existing_checked = False
    existing = None
    if current:
        if current.get("exporter_revision") == "harbor-langfuse-v2":
            # Keep the verified v2 receipt active until the side-effect-free v3 collision/read
            # check succeeds. A transient read error then leaves an exact retryable state.
            existing = _read_back(trace_id, client)
            existing_checked = True
        migrated = _migrate_verified_v2_receipt(trial, current, pending, evidence, client)
        if not migrated:
            return _resume_pending(trial, current, pending, trace_id, digest, evidence, client)
    if not existing_checked:
        existing = _read_back(trace_id, client)
    if existing is not None:
        if not _matching_observation_id(existing, trace_id, digest, evidence):
            raise TelemetryReceiptError(f"existing deterministic trace {trace_id} is not one expected attempt")
        receipt = _expected_receipt(digest)
        _atomic_receipt(trial / RECEIPT_NAME, receipt)
        return receipt
    if not migrated and not _create_pending_receipt(trial / RECEIPT_NAME, pending):
        current = _read_receipt(trial)
        if current == _expected_receipt(digest):
            return current
        return _resume_pending(trial, current, pending, trace_id, digest, evidence, client)
    outbound = _redact_persisted(payload, os.environ)
    # ISO timestamps are validated structurally before this final free-text scrub. The diagnostic
    # host pattern also matches clock fragments, so retain the already-validated canonical values.
    outbound["timestamps"] = payload["timestamps"]
    for outbound_step, canonical_step in zip(
            outbound["trajectory"].get("steps") or [], payload["trajectory"].get("steps") or []):
        if "timestamp" in canonical_step:
            outbound_step["timestamp"] = canonical_step["timestamp"]
    observation_metadata = {
        **outbound["metadata"],
        "attempt": outbound["attempt"],
        "payload_sha256": digest,
        "exporter_revision": EXPORTER_REVISION,
        "telemetry_evidence": evidence,
    }
    observation_output = {key: outbound[key] for key in (
        "skill", "status", "exception_category", "error_detail", "timestamps",
        "trajectory", "verifier_output", "solution_artifacts")}
    if "artifact_projection" in outbound:
        observation_output["artifact_projection"] = outbound["artifact_projection"]
    kwargs = {
        "trace_context": {"trace_id": trace_id},
        "name": "harbor-attempt",
        "as_type": "agent",
        "input": outbound["task"],
        "output": observation_output,
        "metadata": observation_metadata,
        "level": "ERROR" if outbound["status"] == "failed" else "DEFAULT",
        "status_message": outbound["status"],
    }
    observation = client.start_observation(**kwargs)
    observation_id = getattr(observation, "id", None)
    if not isinstance(observation_id, str) or not observation_id.strip():
        raise TelemetryReceiptError("Langfuse returned no observation ID")
    _atomic_receipt(trial / RECEIPT_NAME, _pending_receipt(digest, observation_id))
    observation.end()
    client.flush()
    if not _wait_for_matching_observation(trace_id, digest, evidence, observation_id, client):
        raise TelemetryReceiptError(f"Langfuse trace {trace_id} failed public read-back verification")
    receipt = _expected_receipt(digest)
    _atomic_receipt(trial / RECEIPT_NAME, receipt)
    return receipt


def _is_attempt(path: Path) -> bool:
    with _AttemptReader(path) as reader:
        if (reader.read(
                "agent", "trajectory.json", limit=_MAX_TRAJECTORY_BYTES) is not None
                or reader.read("exception.txt") is not None):
            return True
        opened = reader._open_dir(reader._trial_fd, "verifier", missing_ok=True)
        if opened is not None:
            descriptor, _info = opened
            os.close(descriptor)
            return True
        return isinstance(reader.read_json("result.json").get("task_name"), str)


def _job_attempts(job: Path, identity: NativeTrialIdentity | None = None) -> list[Path]:
    try:
        job_info = job.lstat()
    except OSError:
        return []
    if stat.S_ISLNK(job_info.st_mode) or not stat.S_ISDIR(job_info.st_mode):
        raise TelemetryReceiptError(f"{job} is not a real job directory")
    attempts = []
    for path in sorted(job.iterdir()):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise TelemetryReceiptError(f"{path} is a symlinked attempt entry")
        if not stat.S_ISDIR(info.st_mode):
            continue
        if identity is not None:
            try:
                if read_trial_identity(path) != identity:
                    continue
            except ValueError:
                continue
        if _is_attempt(path):
            attempts.append(path)
    return attempts


def export_job_attempts(job: Path, metadata: Mapping[str, object], *,
                        identity: NativeTrialIdentity | None = None, client=None) -> list[dict]:
    """Export every persisted attempt directly beneath one Harbor job directory."""
    attempts = _job_attempts(job, identity)
    if not attempts:
        raise TelemetryReceiptError(f"{job} has no persisted Harbor attempts")
    return [_export_attempt(trial, metadata, client) for trial in attempts]


def _evidence_attempts(root: Path) -> list[Path]:
    try:
        root_info = root.lstat()
    except OSError:
        return []
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise TelemetryReceiptError(f"{root} is not a real evidence root")
    candidates = {path.parent.parent for path in root.rglob("agent/trajectory.json")}
    candidates.update(path.parent for path in root.rglob("exception.txt"))
    candidates.update(path.parent for path in root.rglob("verifier") if path.is_dir())
    for path in root.rglob("result.json"):
        if isinstance(_path_json(path).get("task_name"), str):
            candidates.add(path.parent)
    return sorted(path for path in candidates if _is_attempt(path))


def _trial_metadata(trial: Path, root: Path,
                    migration: Mapping[str, object] | None = None) -> dict:
    metadata = {}
    for ancestor in trial.parents:
        combo = ancestor / "combo.json"
        if combo.is_file():
            metadata.update(_path_json(combo))
            break
        if ancestor == root:
            break
    if migration:
        metadata.update(migration)
    metadata["arm"] = "canary" if "canaries" in trial.parts else trial.parent.name
    return metadata


def _validate_preserved_provenance(trial: Path, metadata: Mapping[str, object]) -> None:
    skill = metadata.get("skill")
    task_texts = metadata.get("task_texts")
    if not isinstance(skill, str) or not skill.strip() or not isinstance(task_texts, Mapping):
        raise TelemetryReceiptError(f"{trial} lacks explicit preserved provenance")
    body = _known_skill_body(metadata)
    with _AttemptReader(trial) as reader:
        task_name = _task_slug(reader.read_json("result.json").get("task_name") or trial.name)
    task_text = task_texts.get(task_name)
    if body is None or not isinstance(task_text, str) or not task_text.strip():
        raise TelemetryReceiptError(f"{trial} lacks matching preserved provenance")


def export_evidence_root(root: Path, *, metadata: Mapping[str, object] | None = None,
                         client=None) -> list[dict]:
    """Export attempts recursively from preserved Harbor evidence without invoking an agent."""
    attempts = _evidence_attempts(root)
    if not attempts:
        raise TelemetryReceiptError(f"{root} has no persisted Harbor attempts")
    exports = []
    for trial in attempts:
        trial_metadata = _trial_metadata(trial, root, metadata)
        _validate_preserved_provenance(trial, trial_metadata)
        exports.append(_export_attempt(trial, trial_metadata, client))
    return exports


def validate_job_receipts(job: Path, metadata: Mapping[str, object], *,
                          identity: NativeTrialIdentity | None = None) -> None:
    """Fail closed unless every consumed attempt has a current verified receipt."""
    attempts = _job_attempts(job, identity)
    if not attempts:
        raise TelemetryReceiptError(f"{job} has no attempt receipt evidence")
    for trial in attempts:
        _validate_preserved_provenance(trial, metadata)
        digest = _payload_sha256(build_attempt_payload(trial, metadata))
        with _AttemptReader(trial) as reader:
            receipt = reader.read_json(RECEIPT_NAME)
        if receipt != _expected_receipt(digest):
            raise TelemetryReceiptError(f"{trial} has a stale or withheld Langfuse receipt")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", required=True, type=Path,
                        help="preserved Harbor evidence root (repeatable)")
    parser.add_argument("--metadata", action="append", required=True, type=Path,
                        help="bounded provenance migration JSON paired with each --root")
    args = parser.parse_args(argv)
    if len(args.root) != len(args.metadata):
        parser.error("each --root requires one paired --metadata document")
    for root, metadata_path in zip(args.root, args.metadata):
        metadata = load_provenance_metadata(metadata_path)
        export_evidence_root(root, metadata=metadata)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
