"""Normalize local Claude Code and Codex transcripts into trace roots Ingot can mine.

Only the human task, final answer, observed skill identity, timing, token counts, and error count
cross this boundary. Reasoning, tool arguments, tool results, attachments, and injected agent
context stay in the source transcript. Historical revisions are recorded only when an Ingot
`route_and_load` result supplied one; the current skill hash is not evidence of what ran before.

Usage:
    python -m ingot.optimize.local_traces
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from collections import Counter
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Iterable
from ingot import paths

SCHEMA = "ingot/local-traces/v1"
# Cursor reuse is valid only while parser semantics are unchanged. Bump this when accepted record
# shapes, attribution, or usage/error extraction changes; source mtimes cannot invalidate code.
PARSER_VERSION = 3
RUNS_DIR = paths.runs()
LOCAL_TRACE_FILE = Path(os.environ.get(
    "LOCAL_TRACE_FILE", RUNS_DIR / "local_traces.json"))
CODEX_DIR = Path(os.environ.get("CODEX_SESSIONS_DIR", Path.home() / ".codex" / "sessions"))
CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects"))

_INJECTED_CODEX_PREFIXES = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "<codex_internal_context",
    "<recommended_plugins>",
)
_SYNTHETIC_CLAUDE_PREFIXES = (
    "<task-notification>",
    "<system-reminder>",
    "<command-message>",
    "<local-command-stdout>",
    "<command-name>",
    "<user_shell_command>",
    "<bash-stdout>",
    "<subagent_notification>",
)
_SKILL_PATH = re.compile(r"(?:^|[\s\"'=:(])[^\s\"']*/skills/([^/\s\"']+)/SKILL\.md")
_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REVISION = re.compile(r"^[0-9a-f]{8,64}$", re.IGNORECASE)
_MAX_ROUTE_ENVELOPE = 1_000_000
_MAX_ROUTE_DEPTH = 6
_MAX_ROUTE_NODES = 64
_ROUTE_TOOL_NAMES = {"route_and_load", "mcp__ingot__route_and_load"}
_ROUTE_MARKERS = {"revision", "skill_body", "related_match"}
_USAGE_KEYS = (
    "input_tokens", "output_tokens", "cached_input_tokens",
    "cache_write_input_tokens", "reasoning_output_tokens",
)


def _records(path: Path) -> Iterable[dict]:
    try:
        with path.open(errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _content_text(content, block_types: set[str]) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [block.get("text", "") for block in content
             if isinstance(block, dict) and block.get("type") in block_types
             and isinstance(block.get("text"), str)]
    return "\n".join(part.strip() for part in parts if part.strip()).strip()


def _codex_task(payload: dict) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "input_text":
            continue
        text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        stripped = text.lstrip()
        if stripped.startswith(_INJECTED_CODEX_PREFIXES + _SYNTHETIC_CLAUDE_PREFIXES):
            continue
        parts.append(text.strip())
    return "\n".join(parts).strip()


def _valid_skill(value) -> str | None:
    value = value.strip() if isinstance(value, str) else ""
    return value if _SKILL_NAME.fullmatch(value) else None


def _skills_from_command(command: str) -> list[str]:
    return list(dict.fromkeys(
        name for match in _SKILL_PATH.finditer(command)
        if (name := _valid_skill(match.group(1)))
    ))


def _json_value(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _route_identity(value, depth: int = 0,
                    budget: list[int] | None = None) -> tuple[str, str | None] | None:
    """Extract only route identity from nested MCP result shapes; discard the served body."""
    budget = [_MAX_ROUTE_NODES] if budget is None else budget
    if depth > _MAX_ROUTE_DEPTH or budget[0] <= 0:
        return None
    budget[0] -= 1
    if isinstance(value, str) and len(value) > _MAX_ROUTE_ENVELOPE:
        return None
    value = _json_value(value)
    if isinstance(value, dict):
        selected = _valid_skill(value.get("match") or value.get("related_match"))
        if selected and _ROUTE_MARKERS.intersection(value):
            revision = value.get("revision")
            revision = (revision if isinstance(revision, str)
                        and _REVISION.fullmatch(revision) else None)
            return selected, revision
        for key in ("result", "structuredContent", "content", "output"):
            if key in value and (identity := _route_identity(
                    value[key], depth + 1, budget)):
                return identity
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                item = item.get("text")
            if identity := _route_identity(item, depth + 1, budget):
                return identity
    return None


def _merge_skill(skills: dict[str, str | None], name: str, revision: str | None = None) -> None:
    if name not in skills or revision:
        skills[name] = revision


def _tags(skills: dict[str, str | None]) -> list[str]:
    out = []
    for name, revision in skills.items():
        out.append(f"skill:{name}")
        if revision:
            out.append(f"revision={name}@{revision}")
    return out


def _add_usage(total: dict[str, int], usage) -> None:
    if not isinstance(usage, dict):
        return
    values = {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cached_input_tokens": (
            usage.get("cached_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)),
        "cache_write_input_tokens": (
            usage.get("cache_write_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)),
        "reasoning_output_tokens": usage.get("reasoning_output_tokens", 0),
    }
    for key, value in values.items():
        try:
            amount = int(value or 0)
        except (TypeError, ValueError):
            continue
        if amount:
            total[key] = total.get(key, 0) + amount


def _tool_failed(value) -> bool:
    """Count only structured failure signals; free-form output text is not an error contract."""
    value = _json_value(value)
    if not isinstance(value, dict):
        return False
    exit_code = value.get("exit_code")
    try:
        failed_exit = exit_code is not None and int(exit_code) != 0
    except (TypeError, ValueError):
        failed_exit = False
    return failed_exit or value.get("success") is False or value.get("is_error") is True


def _trace(*, harness: str, session_id: str, turn_id: str, timestamp: str, cwd: str,
           task: str, answer: str, skills: dict[str, str | None], usage: dict[str, int],
           duration_ms: int | None = None, tool_errors: int = 0) -> dict:
    identity = json.dumps([harness, session_id, turn_id, task, answer],
                          ensure_ascii=False, separators=(",", ":"))
    result = {
        "id": hashlib.sha256(identity.encode()).hexdigest(),
        "timestamp": timestamp,
        "harness": harness,
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": cwd,
        "project": Path(cwd).name if cwd else "",
        "task": task,
        "rubric": "",
        "answer": answer,
        "skills": [{"name": name, "revision": revision} for name, revision in skills.items()],
        "tags": _tags(skills),
        "usage": {key: usage[key] for key in _USAGE_KEYS if usage.get(key)},
        "tool_errors": tool_errors,
    }
    if duration_ms is not None:
        result["duration_ms"] = duration_ms
    return result


def parse_codex_session(path: Path) -> list[dict]:
    session_id = path.stem
    cwd = ""
    thread_source = ""
    pending_task = ""
    pending_timestamp = ""
    active = None
    traces = []

    for record in _records(path):
        kind, payload = record.get("type"), record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if kind == "session_meta":
            session_id = str(payload.get("id") or payload.get("session_id") or session_id)
            cwd = str(payload.get("cwd") or cwd)
            source = payload.get("thread_source")
            if not source and isinstance(payload.get("source"), dict):
                source = "subagent" if payload["source"].get("subagent") else ""
            thread_source = str(source or "")
            continue
        if thread_source and thread_source != "user":
            continue
        if (kind == "response_item" and payload.get("type") == "message"
                and payload.get("role") == "user"):
            task = _codex_task(payload)
            if task:
                pending_task = task
                pending_timestamp = str(record.get("timestamp") or "")
            continue
        if kind == "event_msg" and payload.get("type") == "task_started":
            active = ({
                "task": pending_task, "timestamp": pending_timestamp,
                "turn_id": str(payload.get("turn_id") or ""),
                "skills": {}, "usage": {}, "route_calls": set(), "tool_errors": 0,
            } if pending_task else None)
            pending_task = pending_timestamp = ""
            continue
        if active is None:
            continue
        if kind == "response_item" and payload.get("type") in {
                "function_call", "custom_tool_call"}:
            name = str(payload.get("name") or "")
            arguments = _json_value(payload.get("arguments"))
            if isinstance(arguments, dict):
                if isinstance(arguments.get("cmd"), str):
                    for skill in _skills_from_command(arguments["cmd"]):
                        _merge_skill(active["skills"], skill)
                if name in _ROUTE_TOOL_NAMES:
                    call_id = str(payload.get("call_id") or "")
                    if call_id:
                        active["route_calls"].add(call_id)
            continue
        if kind == "response_item" and payload.get("type") in {
                "function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id") or "")
            output = payload.get("output")
            if (call_id and call_id in active["route_calls"]
                    and (identity := _route_identity(output))):
                _merge_skill(active["skills"], *identity)
            active["tool_errors"] += int(_tool_failed(output))
            continue
        if kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                _add_usage(active["usage"], info.get("last_token_usage"))
            continue
        if kind == "event_msg" and payload.get("type") == "turn_aborted":
            active = None
            continue
        if kind == "event_msg" and payload.get("type") == "task_complete":
            answer = payload.get("last_agent_message")
            if isinstance(answer, str) and answer.strip():
                duration = payload.get("duration_ms")
                try:
                    duration = int(duration) if duration is not None else None
                except (TypeError, ValueError):
                    duration = None
                traces.append(_trace(
                    harness="codex", session_id=session_id, turn_id=active["turn_id"],
                    timestamp=active["timestamp"], cwd=cwd, task=active["task"],
                    answer=answer.strip(), skills=active["skills"], usage=active["usage"],
                    duration_ms=duration, tool_errors=active["tool_errors"],
                ))
            active = None
    return traces


def _claude_human_task(record: dict) -> str:
    if record.get("type") != "user" or record.get("isSidechain") is True:
        return ""
    # Claude serializes skill-hook output, compaction summaries, local command echoes, and some
    # system injections as role=user. Provenance fields, not message wording, distinguish those
    # records from typed/queued/SDK requests; accepting every user role turns tool output into
    # fabricated tasks and severs the skill call from its real turn.
    if (record.get("isMeta") or record.get("isCompactSummary")
            or record.get("isVisibleInTranscriptOnly") or record.get("sourceToolUseID")):
        return ""
    source = record.get("promptSource")
    if source == "system":
        return ""
    if not record.get("origin") and source not in {
            "typed", "queued", "suggestion_accepted", "sdk"}:
        return ""
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
        return ""
    task = _content_text(content, {"text"})
    return "" if task.lstrip().startswith(_SYNTHETIC_CLAUDE_PREFIXES) else task


def parse_claude_session(path: Path) -> list[dict]:
    traces = []
    active = None
    sequence = 0

    for record in _records(path):
        task = _claude_human_task(record)
        if task:
            active = {
                "task": task,
                "timestamp": str(record.get("timestamp") or ""),
                "session_id": str(record.get("sessionId") or path.stem),
                "turn_id": str(record.get("uuid") or f"turn-{sequence}"),
                "cwd": str(record.get("cwd") or ""),
                "skills": {}, "usage": {}, "route_calls": set(), "tool_errors": 0,
            }
            sequence += 1
            continue
        if active is None or record.get("isSidechain") is True:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if record.get("type") == "assistant":
            _add_usage(active["usage"], message.get("usage"))
            for block in content if isinstance(content, list) else ():
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name, inputs = str(block.get("name") or ""), block.get("input")
                inputs = inputs if isinstance(inputs, dict) else {}
                if name == "Skill" and (skill := _valid_skill(
                        inputs.get("skill") or inputs.get("name"))):
                    _merge_skill(active["skills"], skill)
                if name in _ROUTE_TOOL_NAMES:
                    call_id = str(block.get("id") or "")
                    if call_id:
                        active["route_calls"].add(call_id)
            if message.get("stop_reason") == "end_turn":
                answer = _content_text(content, {"text"})
                if answer:
                    traces.append(_trace(
                        harness="claude", session_id=active["session_id"],
                        turn_id=active["turn_id"], timestamp=active["timestamp"],
                        cwd=active["cwd"], task=active["task"], answer=answer,
                        skills=active["skills"], usage=active["usage"],
                        tool_errors=active["tool_errors"],
                    ))
                    active = None
        elif record.get("type") == "user" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                if block.get("is_error"):
                    active["tool_errors"] += 1
                call_id = str(block.get("tool_use_id") or "")
                if call_id and call_id in active["route_calls"]:
                    if identity := _route_identity(block.get("content")):
                        _merge_skill(active["skills"], *identity)
    return traces


def _jsonl_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.jsonl") if path.is_file())


def _date_value(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError(f"expected an ISO date (YYYY-MM-DD), got {value!r}") from error


def _filters(projects: Iterable[str] = (), since: str = "", until: str = "") -> dict:
    result = {
        "projects": sorted({str(project).strip() for project in projects if str(project).strip()}),
        "since": _date_value(since),
        "until": _date_value(until),
    }
    if result["since"] and result["until"] and result["since"] > result["until"]:
        raise ValueError("--since must be on or before --until")
    return result


def _selected(trace: dict, filters: dict) -> bool:
    projects = filters["projects"]
    if projects and trace.get("project") not in projects:
        return False
    day = str(trace.get("timestamp") or "")[:10]
    if (filters["since"] or filters["until"]) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return False
    if filters["since"] and day < filters["since"]:
        return False
    if filters["until"] and day > filters["until"]:
        return False
    return True


def _source_key(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()


def _prior_store(output: Path, filters: dict) -> dict:
    try:
        payload = json.loads(output.read_text())
    except (OSError, ValueError):
        return {}
    if (payload.get("schema_version") != SCHEMA
            or payload.get("parser_version") != PARSER_VERSION
            or payload.get("filters", _filters()) != filters
            or not isinstance(payload.get("traces"), list)
            or not isinstance(payload.get("source_index"), dict)):
        return {}
    for trace in payload["traces"]:
        if (not isinstance(trace, dict) or not isinstance(trace.get("id"), str)
                or not isinstance(trace.get("_sources"), list)
                or not trace["_sources"]
                or not all(isinstance(source, str) for source in trace["_sources"])):
            return {}
    return payload


def scan(*, codex_dir: Path = CODEX_DIR, claude_dir: Path = CLAUDE_DIR,
         output: Path = LOCAL_TRACE_FILE, projects: Iterable[str] = (),
         since: str = "", until: str = "", force: bool = False) -> dict:
    selected_filters = _filters(projects, since, until)
    files = {"codex": _jsonl_files(codex_dir), "claude": _jsonl_files(claude_dir)}
    prior = {} if force else _prior_store(output, selected_filters)
    prior_index = prior.get("source_index", {})
    by_source: dict[str, list[dict]] = {}
    for trace in prior.get("traces", []):
        if not isinstance(trace, dict):
            continue
        for source in trace.get("_sources", []):
            if isinstance(source, str):
                by_source.setdefault(source, []).append(trace)

    source_index = {"codex": {}, "claude": {}}
    collected = []
    reused = parsed = 0
    parsers = {"codex": parse_codex_session, "claude": parse_claude_session}
    for harness, paths in files.items():
        old = prior_index.get(harness, {}) if isinstance(prior_index, dict) else {}
        old = old if isinstance(old, dict) else {}
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            key = _source_key(path)
            # This cheap cursor fits append-only agent logs. --force is the escape hatch for a
            # same-size rewrite whose mtime was deliberately preserved.
            fingerprint = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            source_index[harness][key] = fingerprint
            if old.get(key) == fingerprint:
                file_traces = by_source.get(key, [])
                fresh = False
                reused += 1
            else:
                file_traces = parsers[harness](path)
                fresh = True
                parsed += 1
            for trace in file_traces:
                if (not isinstance(trace, dict) or not isinstance(trace.get("id"), str)
                        or not _selected(trace, selected_filters)):
                    continue
                copied = dict(trace)
                # Rebuild provenance from files that still exist. Carrying the old list forward
                # would keep a removed duplicate as a live source forever.
                copied["_sources"] = [key]
                copied["_fresh"] = fresh
                collected.append(copied)

    deduped = {}
    for trace in collected:
        existing = deduped.get(trace["id"])
        if existing is None:
            deduped[trace["id"]] = trace
        else:
            sources = sorted(
                set(existing.get("_sources", ())) | set(trace.get("_sources", ())))
            existing_source = min(existing.get("_sources", ("",)))
            candidate_source = min(trace.get("_sources", ("",)))
            if ((trace["_fresh"] and not existing["_fresh"])
                    or (trace["_fresh"] == existing["_fresh"]
                        and candidate_source < existing_source)):
                deduped[trace["id"]] = trace
                existing = trace
            existing["_sources"] = sources
    for trace in deduped.values():
        trace.pop("_fresh", None)
    ordered = sorted(deduped.values(), key=lambda trace: (
        trace.get("timestamp", ""), trace["id"]))
    payload = {
        "schema_version": SCHEMA,
        "parser_version": PARSER_VERSION,
        "generated_at": int(time.time()),
        "filters": selected_filters,
        "sources": {name: {"files": len(paths)} for name, paths in files.items()},
        "source_index": source_index,
        "traces": ordered,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    staged_path = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", dir=output.parent, prefix=f".{output.name}.", delete=False) as staged:
            staged_path = Path(staged.name)
            json.dump(payload, staged, ensure_ascii=False, separators=(",", ":"))
            staged.write("\n")
            staged.flush()
            os.fsync(staged.fileno())
        os.replace(staged_path, output)
        staged_path = None
        directory = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
    return {"output": str(output), "traces": len(ordered),
            "files": sum(len(paths) for paths in files.values()),
            "parsed_files": parsed, "reused_files": reused}


def _empty_summary(status: str = "missing") -> dict:
    return {
        "configured": False, "status": status, "generated_at": None,
        "total": 0, "harnesses": {},
        "available_total": 0, "available_projects": {}, "available_harnesses": {},
        "available_skills": {},
        "filters": {"projects": [], "harness": "", "skill": "", "since": "", "until": "",
                    "include_tasks": False},
        "skill_uses": {}, "revision_pinned": 0, "unattributed": 0,
        "usage": {}, "tool_errors": 0, "recent": [],
    }


@lru_cache(maxsize=1)
def _read_store(path: str, mtime_ns: int, size: int) -> dict:
    del mtime_ns, size  # cache-key material; the file path is the only read target
    return json.loads(Path(path).read_text())


def _observed_skills(trace: dict) -> set[str]:
    """Validated skill names attributed to one turn."""
    entries = trace.get("skills") if isinstance(trace.get("skills"), list) else []
    return {name for skill in entries if isinstance(skill, dict)
            and (name := _valid_skill(skill.get("name")))}


def store_summary(path: Path = LOCAL_TRACE_FILE, recent: int = 20, *, project: str = "",
                  harness: str = "", skill: str = "", since: str = "", until: str = "",
                  include_tasks: bool = False) -> dict:
    try:
        stat = path.stat()
    except OSError:
        return _empty_summary()
    try:
        payload = _read_store(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError):
        return _empty_summary("unreadable")
    if payload.get("schema_version") != SCHEMA or not isinstance(payload.get("traces"), list):
        return _empty_summary("unreadable")
    all_traces = [trace for trace in payload["traces"] if isinstance(trace, dict)]
    available_projects = Counter(
        str(trace.get("project")) for trace in all_traces if trace.get("project"))
    available_harnesses = Counter(
        str(trace.get("harness")) for trace in all_traces if trace.get("harness"))
    # Counted over every turn, not the filtered set: the picker has to keep offering a skill after
    # you select it, and offering only skills that survive the current filter would empty itself.
    available_skills = Counter(
        name for trace in all_traces for name in _observed_skills(trace))
    selected_filters = _filters([project] if project else (), since, until)
    traces = [trace for trace in all_traces
              if _selected(trace, selected_filters)
              and (not harness or trace.get("harness") == harness)
              and (not skill or skill in _observed_skills(trace))]
    harnesses = Counter()
    skill_uses = Counter()
    usage = Counter()
    pinned = tool_errors = unattributed = 0
    for trace in traces:
        if not isinstance(trace, dict):
            continue
        harnesses[str(trace.get("harness") or "unknown")] += 1
        skills = trace.get("skills") if isinstance(trace.get("skills"), list) else []
        if not skills:
            unattributed += 1
        for observed in skills:   # not `skill`: that name is the filter parameter
            if not isinstance(observed, dict) or not _valid_skill(observed.get("name")):
                continue
            skill_uses[observed["name"]] += 1
            pinned += int(bool(observed.get("revision")))
        for key in _USAGE_KEYS:
            try:
                usage[key] += int((trace.get("usage") or {}).get(key, 0))
            except (AttributeError, TypeError, ValueError):
                pass
        try:
            tool_errors += int(trace.get("tool_errors") or 0)
        except (TypeError, ValueError):
            pass
    newest = sorted(
        (trace for trace in traces if isinstance(trace, dict)),
        key=lambda trace: (trace.get("timestamp", ""), trace.get("id", "")), reverse=True,
    )[:recent]
    previews = []
    for trace in newest:
        preview_skills = []
        for observed in trace.get("skills", []) if isinstance(trace.get("skills"), list) else ():
            if not isinstance(observed, dict) or not (name := _valid_skill(observed.get("name"))):
                continue
            revision = observed.get("revision")
            revision = (revision if isinstance(revision, str)
                        and _REVISION.fullmatch(revision) else None)
            preview_skills.append({"name": name, "revision": revision})
        preview_usage = {}
        stored_usage = trace.get("usage") if isinstance(trace.get("usage"), dict) else {}
        for key in _USAGE_KEYS:
            try:
                amount = int(stored_usage.get(key, 0))
            except (TypeError, ValueError):
                continue
            if amount > 0:
                preview_usage[key] = amount
        try:
            preview_errors = max(0, int(trace.get("tool_errors") or 0))
        except (TypeError, ValueError):
            preview_errors = 0
        preview = {
            "id": str(trace.get("id") or ""),
            "timestamp": str(trace.get("timestamp") or ""),
            "harness": str(trace.get("harness") or ""),
            "project": str(trace.get("project") or ""),
            "skills": preview_skills,
            "usage": preview_usage,
            "tool_errors": preview_errors,
        }
        if include_tasks:
            task = str(trace.get("task") or "")
            preview["task"] = task[:280] + ("…" if len(task) > 280 else "")
        previews.append(preview)
    return {
        "configured": True, "status": "ready", "generated_at": payload.get("generated_at"),
        "available_total": len(all_traces),
        "available_projects": dict(available_projects.most_common()),
        "available_harnesses": dict(available_harnesses.most_common()),
        "available_skills": dict(available_skills.most_common()),
        "filters": {**selected_filters, "harness": harness, "skill": skill,
                    "include_tasks": include_tasks},
        "total": len(traces), "harnesses": dict(harnesses),
        "skill_uses": dict(skill_uses.most_common()), "revision_pinned": pinned,
        "unattributed": unattributed, "usage": dict(usage),
        "tool_errors": tool_errors, "recent": previews,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-dir", type=Path, default=CODEX_DIR)
    parser.add_argument("--claude-dir", type=Path, default=CLAUDE_DIR)
    parser.add_argument("--output", type=Path, default=LOCAL_TRACE_FILE)
    parser.add_argument("--project", action="append", default=[],
                        help="keep only this cwd basename; repeat for more than one project")
    parser.add_argument("--since", default="", help="keep turns on or after YYYY-MM-DD")
    parser.add_argument("--until", default="", help="keep turns on or before YYYY-MM-DD")
    parser.add_argument("--force", action="store_true",
                        help="reparse every transcript instead of reusing the cursor")
    args = parser.parse_args()
    try:
        result = scan(codex_dir=args.codex_dir, claude_dir=args.claude_dir, output=args.output,
                      projects=args.project, since=args.since, until=args.until, force=args.force)
    except ValueError as error:
        parser.error(str(error))
    print(f"[traces] normalized {result['traces']} completed turn(s) from "
          f"{result['files']} transcript file(s) "
          f"({result['parsed_files']} parsed, {result['reused_files']} unchanged) "
          f"→ {result['output']}")


if __name__ == "__main__":
    main()
