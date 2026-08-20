"""Fail-closed subprocess adapter for the subscription-backed Agy judge."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Mapping, Sequence


AGY_MODEL = "gemini-3.6-flash-medium"
AGY_IDENTITY = f"agy/{AGY_MODEL}"

_VERDICTS = ("pass", "partial", "fail")
_PROVIDER_PREFIXES = (
    "OPENROUTER_",
    "OPENAI_",
    "ANTHROPIC_",
    "GEMINI_",
    "GOOGLE_",
    "VERTEX_",
)
_PROVIDER_KEYS = frozenset({"BASE_URL", "API_KEY", "MODEL_API_KEY"})
_LAUNCH_INTERVAL_SECONDS = 3.2
_RESOURCE_EXHAUSTED_RETRIES = 3
_launch_lock = threading.Lock()
_next_launch_at = 0.0


class AgyJudgeError(RuntimeError):
    """Agy did not produce one trustworthy checklist grade."""


def agy_process_env(parent: Mapping[str, str]) -> dict[str, str]:
    """Copy the parent environment without provider credentials or routing controls."""
    child = {
        key: value
        for key, value in parent.items()
        if key not in _PROVIDER_KEYS and not key.startswith(_PROVIDER_PREFIXES)
    }
    child["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    return child


def _checklist_ids(checklist: Sequence[Mapping[str, object]]) -> list[str]:
    ids = [item.get("id") for item in checklist]
    if not ids or any(not isinstance(item_id, str) or not item_id for item_id in ids):
        raise AgyJudgeError("Agy checklist requires non-empty string IDs")
    if len(set(ids)) != len(ids):
        raise AgyJudgeError("Agy checklist IDs must be unique")
    return ids


def judge_schema(checklist: Sequence[Mapping[str, object]]) -> dict:
    """Build the strict Agy output schema for the exact checklist IDs."""
    ids = _checklist_ids(checklist)
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "note"],
        "properties": {
            "verdict": {"enum": list(_VERDICTS)},
            "note": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["items", "feedback"],
        "properties": {
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ids,
                "properties": {item_id: item_schema.copy() for item_id in ids},
            },
            "feedback": {"type": "string"},
        },
    }


def _validate_grade(raw: object, checklist: Sequence[Mapping[str, object]]) -> dict:
    ids = _checklist_ids(checklist)
    if not isinstance(raw, dict):
        raise AgyJudgeError("Agy result has no structured output")
    if set(raw) != {"items", "feedback"} or not isinstance(raw.get("feedback"), str):
        raise AgyJudgeError("Agy structured output has an invalid top-level shape")
    items = raw.get("items")
    if not isinstance(items, dict) or set(items) != set(ids):
        raise AgyJudgeError("Agy structured output does not match the checklist IDs")
    for item_id in ids:
        item = items[item_id]
        if not isinstance(item, dict) or set(item) != {"verdict", "note"}:
            raise AgyJudgeError(f"Agy checklist item {item_id!r} has an invalid shape")
        if item["verdict"] not in _VERDICTS:
            raise AgyJudgeError(f"Agy checklist item {item_id!r} has an unknown verdict")
        if not isinstance(item["note"], str):
            raise AgyJudgeError(f"Agy checklist item {item_id!r} has an invalid note")
    return raw


def parse_stream(
    stdout: str,
    checklist: Sequence[Mapping[str, object]],
) -> tuple[dict, dict]:
    """Return the sole successful terminal grade and usage from an Agy JSONL stream."""
    terminal = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AgyJudgeError(f"Agy emitted malformed JSONL on line {line_number}") from exc
        if not isinstance(event, dict):
            raise AgyJudgeError(f"Agy emitted a non-object event on line {line_number}")
        if event.get("event") == "result":
            terminal.append(event)
    if len(terminal) != 1:
        raise AgyJudgeError(f"Agy emitted {len(terminal)} terminal results; expected one")

    result = terminal[0].get("result")
    if not isinstance(result, dict):
        raise AgyJudgeError("Agy terminal result has an invalid shape")
    if result.get("status") != "SUCCESS":
        raise AgyJudgeError("Agy terminal result was not successful")
    grade = _validate_grade(result.get("structured_output"), checklist)
    usage = result.get("usage")
    if not isinstance(usage, dict):
        raise AgyJudgeError("Agy terminal result has no usage object")
    return grade, usage


def _runtime_paths() -> tuple[Path, Path]:
    agy_value = os.environ.get("AGY_BIN", "").strip()
    workspace_value = os.environ.get("AGY_JUDGE_WORKSPACE", "").strip()
    if not agy_value or not workspace_value:
        raise AgyJudgeError("AGY_BIN and AGY_JUDGE_WORKSPACE must be set")
    agy_bin = Path(agy_value)
    workspace = Path(workspace_value)
    if not agy_bin.is_absolute() or not workspace.is_absolute():
        raise AgyJudgeError("Agy runtime paths must be absolute")
    if not agy_bin.is_file() or not os.access(agy_bin, os.X_OK):
        raise AgyJudgeError("AGY_BIN is not an executable file")
    if not workspace.is_dir():
        raise AgyJudgeError("AGY_JUDGE_WORKSPACE is not a directory")
    return agy_bin, workspace


def invoke(
    prompt: str,
    checklist: Sequence[Mapping[str, object]],
    *,
    timeout: float = 120.0,
) -> tuple[dict, dict]:
    """Invoke Agy once and return no grade unless its complete contract validates."""
    agy_bin, workspace = _runtime_paths()
    schema = judge_schema(checklist)
    argv = [
        str(agy_bin),
        "--model", AGY_MODEL,
        "--print", prompt,
        "--output-format", "stream-json",
        "--json-schema", json.dumps(schema),
        "--sandbox",
        "--mode", "plan",
        "--disable-slash-commands",
        "--print-timeout", f"{int(timeout)}s",
    ]
    for attempt in range(_RESOURCE_EXHAUSTED_RETRIES + 1):
        _wait_for_launch_slot()
        try:
            done = subprocess.run(
                argv,
                cwd=workspace,
                env=agy_process_env(os.environ),
                text=True,
                capture_output=True,
                timeout=timeout + 10,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgyJudgeError("Agy judge timed out") from exc
        except OSError as exc:
            raise AgyJudgeError("Agy judge process could not start") from exc
        if done.returncode == 0:
            return parse_stream(done.stdout, checklist)
        if not _resource_exhausted(done.stdout) or attempt == _RESOURCE_EXHAUSTED_RETRIES:
            raise AgyJudgeError(f"Agy judge exited with status {done.returncode}")
    raise AssertionError("unreachable")


def _wait_for_launch_slot() -> None:
    """Keep the subscription eligibility gate below its observed burst limit."""
    global _next_launch_at
    with _launch_lock:
        now = time.monotonic()
        delay = max(0.0, _next_launch_at - now)
        if delay:
            time.sleep(delay)
            now = time.monotonic()
        _next_launch_at = now + _LAUNCH_INTERVAL_SECONDS


def _resource_exhausted(stdout: str) -> bool:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = event.get("result") if isinstance(event, dict) else None
        if not isinstance(result, dict) or result.get("status") != "ERROR":
            continue
        error = str(result.get("error", ""))
        if "RESOURCE_EXHAUSTED" in error and "429" in error:
            return True
    return False


def _run_preflight(argv: list[str], workspace: Path) -> str:
    try:
        done = subprocess.run(
            argv,
            cwd=workspace,
            env=agy_process_env(os.environ),
            text=True,
            capture_output=True,
            timeout=20.0,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AgyJudgeError("Agy preflight timed out") from exc
    except OSError as exc:
        raise AgyJudgeError("Agy preflight process could not start") from exc
    if done.returncode != 0:
        raise AgyJudgeError(f"Agy preflight exited with status {done.returncode}")
    if not done.stdout.strip():
        raise AgyJudgeError("Agy preflight returned no output")
    return done.stdout.strip()


def preflight() -> dict[str, object]:
    """Verify the explicit runtime and return its fixed judge provenance."""
    agy_bin, workspace = _runtime_paths()
    version = _run_preflight([str(agy_bin), "--version"], workspace)
    models = _run_preflight([str(agy_bin), "models"], workspace)
    if AGY_MODEL not in models.split():
        raise AgyJudgeError(f"Agy model {AGY_MODEL!r} is unavailable")
    return {
        "identity": AGY_IDENTITY,
        "model": AGY_MODEL,
        "version": version,
        "billing_mode": "subscription",
    }
