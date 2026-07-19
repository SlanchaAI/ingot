"""Privacy-aware local trace storage with backward-compatible JSONL records."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

SCHEMA_VERSION = 1
_JSON_SECRET = re.compile(
    r'(?i)("(?:api[_-]?key|token|password|secret)"\s*:\s*")'
    r'(?:\\.|[^"\\])*(")'
)
_SECRET = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+|(?:api[_-]?key|token|password|secret)\s*[=:]\s*)"
    r"([^\s,;]+)"
)


def _enabled(name: str, default: bool = True) -> bool:
    return os.environ.get(name, str(default)).strip().lower() not in {"0", "false", "no", "off"}


def redact(value: str) -> str:
    if not _enabled("LOCAL_TRACE_REDACT", True):
        return value
    value = _JSON_SECRET.sub(
        lambda match: f"{match.group(1)}[REDACTED]{match.group(2)}", value)
    return _SECRET.sub(lambda match: f"{match.group(1)}[REDACTED]", value)


def _rotation_due(path: Path, max_bytes: int) -> bool:
    if not path.exists() or not max_bytes:
        return False
    return path.stat().st_size >= max_bytes


def configured_trace_files(path: Path, oldest_first: bool = False) -> list[Path]:
    """Active trace file and configured backups, optionally in chronological file order."""
    backups = max(0, int(os.environ.get("LOCAL_TRACE_BACKUPS", "3")))
    rotated = [path.with_name(f"{path.name}.{number}")
               for number in range(1, backups + 1)]
    return [*reversed(rotated), path] if oldest_first else [path, *rotated]


def _rotate(path: Path) -> None:
    max_bytes = max(0, int(os.environ.get("LOCAL_TRACE_MAX_BYTES", "10485760")))
    backups = max(0, int(os.environ.get("LOCAL_TRACE_BACKUPS", "3")))
    if not _rotation_due(path, max_bytes):
        return
    if backups == 0:
        path.unlink()
        return
    oldest = path.with_name(f"{path.name}.{backups}")
    oldest.unlink(missing_ok=True)
    for number in range(backups - 1, 0, -1):
        source = path.with_name(f"{path.name}.{number}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{number + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))


def _parse_trace_record(line: str) -> dict | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _retain_trace_line(line: str, cutoff: int) -> bool:
    record = _parse_trace_record(line)
    timestamp = record.get("ts") if record is not None else None
    return not isinstance(timestamp, (int, float)) or timestamp >= cutoff


def _rewrite_traces(path: Path, lines: list[str]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.retention.tmp")
    temporary.write_text("\n".join(lines) + ("\n" if lines else ""))
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _expire_file(path: Path, cutoff: int, remove_empty: bool) -> None:
    if not path.exists():
        return
    lines = path.read_text().splitlines()
    kept = [line for line in lines if _retain_trace_line(line, cutoff)]
    if not kept and remove_empty:
        path.unlink()
    elif len(kept) != len(lines):
        _rewrite_traces(path, kept)


def _expire(path: Path) -> None:
    days = max(0, int(os.environ.get("LOCAL_TRACE_MAX_AGE_DAYS", "30")))
    if not days:
        return
    cutoff = int(time.time()) - days * 86400
    for trace_path in configured_trace_files(path):
        _expire_file(trace_path, cutoff, remove_empty=trace_path != path)


def _write_all(fd: int, data: bytes) -> None:
    """Write one complete record. The local trace store has a single writer by contract."""
    remaining = memoryview(data)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("local trace write made no progress")
        remaining = remaining[written:]


def write(task: str, answer: str, tags: list[str]) -> bool:
    """Append a versioned record. Failures and an explicit opt-out never affect serving."""
    if not _enabled("LOCAL_TRACE_ENABLED", True):
        return False
    from optimize import traces_file
    path = traces_file()
    try:
        path.parent.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        pass
    else:
        os.chmod(path.parent, 0o700)
    _expire(path)
    _rotate(path)
    record = {"schema_version": SCHEMA_VERSION, "ts": int(time.time()),
              "task": redact(task), "answer": redact(answer), "tags": tags}
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode())
    finally:
        os.close(fd)
    return True
