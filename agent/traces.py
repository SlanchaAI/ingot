"""Privacy-aware local trace storage with backward-compatible JSONL records."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

SCHEMA_VERSION = 1
_SECRET = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+|(?:api[_-]?key|token|password|secret)\s*[=:]\s*)"
    r"([^\s,;]+)"
)


def _enabled(name: str, default: bool = True) -> bool:
    return os.environ.get(name, str(default)).strip().lower() not in {"0", "false", "no", "off"}


def redact(value: str) -> str:
    if not _enabled("LOCAL_TRACE_REDACT", True):
        return value
    return _SECRET.sub(lambda match: f"{match.group(1)}[REDACTED]", value)


def _rotation_due(path: Path, max_bytes: int) -> bool:
    if not path.exists() or not max_bytes:
        return False
    return path.stat().st_size >= max_bytes


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
    return record is None or record.get("ts", cutoff) >= cutoff


def _rewrite_traces(path: Path, lines: list[str]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.retention.tmp")
    temporary.write_text("\n".join(lines) + ("\n" if lines else ""))
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _expire(path: Path) -> None:
    days = max(0, int(os.environ.get("LOCAL_TRACE_MAX_AGE_DAYS", "30")))
    if not path.exists() or not days:
        return
    cutoff = int(time.time()) - days * 86400
    lines = path.read_text().splitlines()
    kept = [line for line in lines if _retain_trace_line(line, cutoff)]
    if len(kept) != len(lines):
        _rewrite_traces(path, kept)


def write(task: str, answer: str, tags: list[str]) -> bool:
    """Append a versioned record. Failures and an explicit opt-out never affect serving."""
    if not _enabled("LOCAL_TRACE_ENABLED", True):
        return False
    from optimize import traces_file
    path = traces_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    _expire(path)
    _rotate(path)
    record = {"schema_version": SCHEMA_VERSION, "ts": int(time.time()),
              "task": redact(task), "answer": redact(answer), "tags": tags}
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return True
