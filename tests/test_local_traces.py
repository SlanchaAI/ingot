import json
import os
from pathlib import Path

import pytest

from ingot.optimize import local_traces


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def test_codex_parser_ignores_injected_context_and_keeps_completed_human_turn(tmp_path):
    session = tmp_path / "rollout.jsonl"
    records = [
        {"timestamp": "2026-07-28T10:00:00Z", "type": "session_meta", "payload": {
            "id": "codex-session", "cwd": "/work/project", "thread_source": "user",
        }},
        {"timestamp": "2026-07-28T10:00:01Z", "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "# AGENTS.md instructions\ninternal"},
                {"type": "input_text", "text": "<environment_context>internal</environment_context>"},
            ],
        }},
        {"timestamp": "2026-07-28T10:01:00Z", "type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Build the landing page."}],
        }},
        {"timestamp": "2026-07-28T10:01:00Z", "type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text",
                         "text": "<task-notification>synthetic status</task-notification>"}],
        }},
        {"timestamp": "2026-07-28T10:01:01Z", "type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "turn-1", "started_at": 1000,
        }},
        {"timestamp": "2026-07-28T10:01:02Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command", "call_id": "call-1",
            "arguments": json.dumps({
                "cmd": "sed -n '1,220p' /Users/example/.agents/skills/frontend-design/SKILL.md",
            }),
        }},
        {"timestamp": "2026-07-28T10:01:02Z", "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "call-1",
            "output": json.dumps({"exit_code": 1, "output": "failed"}),
        }},
        {"timestamp": "2026-07-28T10:01:02Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "mcp__ingot__route_and_load",
            "call_id": "route-1", "arguments": json.dumps({"task": "Build the landing page."}),
        }},
        {"timestamp": "2026-07-28T10:01:02Z", "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "route-1",
            "output": json.dumps({"match": "web-design-flow", "revision": "83a75cf1",
                                  "skill_body": "private"}),
        }},
        {"timestamp": "2026-07-28T10:01:02Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "route_and_load", "arguments": "{}",
        }},
        {"timestamp": "2026-07-28T10:01:02Z", "type": "response_item", "payload": {
            "type": "function_call_output",
            "output": json.dumps({"match": "wrong-skill", "revision": "deadbeef"}),
        }},
        {"timestamp": "2026-07-28T10:01:03Z", "type": "event_msg", "payload": {
            "type": "token_count", "info": {"last_token_usage": {
                "input_tokens": 120, "output_tokens": 30,
                "cached_input_tokens": 20, "reasoning_output_tokens": 4,
            }},
        }},
        {"timestamp": "2026-07-28T10:01:04Z", "type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "turn-1", "duration_ms": 3000,
            "completed_at": 4000, "last_agent_message": "Landing page shipped.",
        }},
        {"timestamp": "2026-07-28T10:02:00Z", "type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Cancel this request."}],
        }},
        {"timestamp": "2026-07-28T10:02:01Z", "type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "turn-2", "started_at": 5000,
        }},
        {"timestamp": "2026-07-28T10:02:02Z", "type": "event_msg", "payload": {
            "type": "turn_aborted", "turn_id": "turn-2", "reason": "interrupted",
        }},
    ]
    _write_jsonl(session, records)

    traces = local_traces.parse_codex_session(session)

    assert len(traces) == 1
    assert traces[0]["task"] == "Build the landing page."
    assert traces[0]["answer"] == "Landing page shipped."
    assert traces[0]["harness"] == "codex"
    assert traces[0]["session_id"] == "codex-session"
    assert traces[0]["turn_id"] == "turn-1"
    assert traces[0]["cwd"] == "/work/project"
    assert traces[0]["skills"] == [
        {"name": "frontend-design", "revision": None},
        {"name": "web-design-flow", "revision": "83a75cf1"},
    ]
    assert traces[0]["tags"] == [
        "skill:frontend-design", "skill:web-design-flow",
        "revision=web-design-flow@83a75cf1",
    ]
    assert traces[0]["usage"] == {
        "input_tokens": 120, "output_tokens": 30,
        "cached_input_tokens": 20, "reasoning_output_tokens": 4,
    }
    assert traces[0]["duration_ms"] == 3000
    assert traces[0]["tool_errors"] == 1


def test_claude_parser_uses_final_answer_and_skill_tool_without_tool_payloads(tmp_path):
    session = tmp_path / "claude.jsonl"
    records = [
        {"timestamp": "2026-07-28T11:00:00Z", "type": "user", "sessionId": "claude-session",
         "cwd": "/work/project", "isSidechain": False, "userType": "external",
         "promptSource": "typed", "origin": "terminal",
         "message": {"role": "user", "content": "Review this interface."}},
        {"timestamp": "2026-07-28T11:00:01Z", "type": "assistant",
         "sessionId": "claude-session", "cwd": "/work/project", "isSidechain": False,
         "message": {"role": "assistant", "stop_reason": "tool_use",
                     "usage": {"input_tokens": 80, "output_tokens": 10,
                               "cache_read_input_tokens": 5},
                     "content": [{"type": "tool_use", "id": "tool-1", "name": "Skill",
                                  "input": {"skill": "saas-interface-review", "args": ""}}]}},
        {"timestamp": "2026-07-28T11:00:02Z", "type": "user",
         "sessionId": "claude-session", "cwd": "/work/project", "isSidechain": False,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tool-1",
             "content": "private skill instructions that must not enter the trace"},
         ]}},
        {"timestamp": "2026-07-28T11:00:02Z", "type": "user",
         "sessionId": "claude-session", "cwd": "/work/project", "isSidechain": False,
         "isMeta": True, "sourceToolUseID": "tool-1",
         "message": {"role": "user", "content": [
             {"type": "text", "text": "Base directory for this skill: /private/path"},
         ]}},
        {"timestamp": "2026-07-28T11:00:02Z", "type": "user",
         "sessionId": "claude-session", "cwd": "/work/project", "isSidechain": False,
         "promptSource": "queued",
         "message": {"role": "user", "content": [
             {"type": "text", "text": "<system-reminder>synthetic status</system-reminder>"},
         ]}},
        {"timestamp": "2026-07-28T11:00:03Z", "type": "assistant",
         "sessionId": "claude-session", "cwd": "/work/project", "isSidechain": False,
         "message": {"role": "assistant", "stop_reason": "end_turn", "usage": {},
                     "content": [{"type": "thinking", "thinking": "private reasoning"}]}},
        {"timestamp": "2026-07-28T11:00:03Z", "type": "assistant",
         "sessionId": "claude-session", "cwd": "/work/project", "isSidechain": False,
         "message": {"role": "assistant", "stop_reason": "end_turn",
                     "usage": {"input_tokens": 30, "output_tokens": 20,
                               "cache_read_input_tokens": 2},
                     "content": [{"type": "text", "text": "The hierarchy needs work."}]}},
    ]
    _write_jsonl(session, records)

    traces = local_traces.parse_claude_session(session)

    assert len(traces) == 1
    assert traces[0]["task"] == "Review this interface."
    assert traces[0]["answer"] == "The hierarchy needs work."
    assert traces[0]["skills"] == [{"name": "saas-interface-review", "revision": None}]
    assert traces[0]["tags"] == ["skill:saas-interface-review"]
    assert traces[0]["usage"] == {
        "input_tokens": 110, "output_tokens": 30, "cached_input_tokens": 7,
    }
    assert "private skill instructions" not in json.dumps(traces[0])


def test_claude_parser_pins_ingot_route_revision_from_matching_tool_result(tmp_path):
    session = tmp_path / "claude-route.jsonl"
    records = [
        {"timestamp": "2026-07-28T12:00:00Z", "type": "user", "sessionId": "s1",
         "cwd": "/work", "isSidechain": False, "userType": "external",
         "promptSource": "typed", "origin": "terminal",
         "message": {"role": "user", "content": "Merge the PDFs."}},
        {"timestamp": "2026-07-28T12:00:01Z", "type": "assistant", "sessionId": "s1",
         "cwd": "/work", "isSidechain": False, "message": {
             "role": "assistant", "stop_reason": "tool_use", "usage": {},
             "content": [{"type": "tool_use", "id": "route-1",
                          "name": "mcp__ingot__route_and_load",
                          "input": {"task": "Merge the PDFs.", "harness": "claude", "cwd": "/work"}}],
         }},
        {"timestamp": "2026-07-28T12:00:02Z", "type": "user", "sessionId": "s1",
         "cwd": "/work", "isSidechain": False, "userType": "external", "message": {
             "role": "user", "content": [{"type": "tool_result", "tool_use_id": "route-1",
                 "content": json.dumps({"match": "pdf", "related_match": None,
                                        "revision": "83a75cf1", "skill_body": "private"})}],
         }},
        {"timestamp": "2026-07-28T12:00:03Z", "type": "assistant", "sessionId": "s1",
         "cwd": "/work", "isSidechain": False, "message": {
             "role": "assistant", "stop_reason": "end_turn", "usage": {},
             "content": [{"type": "text", "text": "Merged."}],
         }},
    ]
    _write_jsonl(session, records)

    traces = local_traces.parse_claude_session(session)

    assert traces[0]["skills"] == [{"name": "pdf", "revision": "83a75cf1"}]
    assert traces[0]["tags"] == ["skill:pdf", "revision=pdf@83a75cf1"]
    assert "skill_body" not in json.dumps(traces[0])


def test_route_identity_bounds_nested_envelopes_and_revision_content():
    assert local_traces._route_identity({
        "related_match": "pdf", "revision": "83a75cf1",
    }) == ("pdf", "83a75cf1")
    assert local_traces._route_identity({
        "match": "pdf", "revision": {"secret": "must not become a tag"},
    }) == ("pdf", None)
    nested = {"match": "pdf", "revision": "83a75cf1"}
    for _ in range(local_traces._MAX_ROUTE_DEPTH + 1):
        nested = {"result": nested}
    assert local_traces._route_identity(nested) is None
    assert local_traces._route_identity("x" * (local_traces._MAX_ROUTE_ENVELOPE + 1)) is None
    assert local_traces._route_identity({
        "content": [{"type": "text", "text": json.dumps({"match": "not-an-envelope"})}],
    }) is None
    assert local_traces._route_identity({"match": "pdf", "score": 0.99}) is None
    wide = [{"type": "text", "text": "{}"}] * local_traces._MAX_ROUTE_NODES
    wide.append({"type": "text", "text": json.dumps({
        "match": "pdf", "revision": "83a75cf1",
    })})
    assert local_traces._route_identity(wide) is None
    assert local_traces._tool_failed({"exit_code": "0"}) is False
    assert local_traces._tool_failed({"exit_code": "2"}) is True
    assert local_traces._tool_failed({"error": "documented payload field"}) is False


def test_scan_writes_deduplicated_store_and_summary_without_answers(tmp_path):
    codex = tmp_path / "codex"
    claude = tmp_path / "claude"
    output = tmp_path / "runs" / "local_traces.json"
    codex_record = [
        {"timestamp": "2026-07-28T10:00:00Z", "type": "session_meta", "payload": {
            "id": "s", "cwd": "/work", "thread_source": "user",
        }},
        {"timestamp": "2026-07-28T10:00:01Z", "type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Task"}],
        }},
        {"timestamp": "2026-07-28T10:00:02Z", "type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "t",
        }},
        {"timestamp": "2026-07-28T10:00:03Z", "type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "t", "last_agent_message": "Secret answer",
        }},
    ]
    _write_jsonl(codex / "one.jsonl", codex_record)
    _write_jsonl(codex / "copy.jsonl", codex_record)

    result = local_traces.scan(codex_dir=codex, claude_dir=claude, output=output)
    saved = json.loads(output.read_text())
    summary = local_traces.store_summary(output)

    assert result["traces"] == 1
    assert saved["schema_version"] == "ingot/local-traces/v1"
    assert len(saved["traces"]) == 1
    assert summary["configured"] is True
    assert summary["total"] == 1
    assert summary["harnesses"] == {"codex": 1}
    assert "task" not in summary["recent"][0]
    assert local_traces.store_summary(output, include_tasks=True)["recent"][0]["task"] == "Task"
    assert "answer" not in summary["recent"][0]
    assert "Secret answer" not in json.dumps(summary)


def test_scan_reuses_unchanged_files_and_reparses_changed_files(tmp_path, monkeypatch):
    codex = tmp_path / "codex"
    claude = tmp_path / "claude"
    output = tmp_path / "local_traces.json"
    session = codex / "one.jsonl"
    _write_jsonl(session, [
        {"timestamp": "2026-07-28T10:00:00Z", "type": "session_meta",
         "payload": {"id": "s", "cwd": "/work/project", "thread_source": "user"}},
        {"timestamp": "2026-07-28T10:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "Task"}]}},
        {"timestamp": "2026-07-28T10:00:02Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "t"}},
        {"timestamp": "2026-07-28T10:00:03Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "t",
                     "last_agent_message": "Answer"}},
    ])
    first = local_traces.scan(codex_dir=codex, claude_dir=claude, output=output)
    assert first["parsed_files"] == 1 and first["reused_files"] == 0

    monkeypatch.setattr(
        local_traces, "parse_codex_session",
        lambda _path: pytest.fail("unchanged transcript must come from the cursor"),
    )
    second = local_traces.scan(codex_dir=codex, claude_dir=claude, output=output)
    assert second["parsed_files"] == 0 and second["reused_files"] == 1
    assert second["traces"] == 1

    calls = []
    monkeypatch.setattr(
        local_traces, "parse_codex_session",
        lambda path: calls.append(path) or [],
    )
    with session.open("a") as handle:
        handle.write(" \n")
    os.utime(session, None)
    third = local_traces.scan(codex_dir=codex, claude_dir=claude, output=output)
    assert calls == [session]
    assert third["parsed_files"] == 1 and third["reused_files"] == 0

    calls.clear()
    fourth = local_traces.scan(
        codex_dir=codex, claude_dir=claude, output=output, force=True)
    assert calls == [session]
    assert fourth["parsed_files"] == 1 and fourth["reused_files"] == 0


def test_scan_rebuilds_malformed_prior_cursor_instead_of_crashing(tmp_path):
    codex = tmp_path / "codex"
    claude = tmp_path / "claude"
    output = tmp_path / "local_traces.json"
    _write_jsonl(codex / "one.jsonl", [])
    output.write_text(json.dumps({
        "schema_version": local_traces.SCHEMA,
        "parser_version": local_traces.PARSER_VERSION,
        "filters": {"projects": [], "since": "", "until": ""},
        "source_index": {"codex": {}, "claude": {}},
        "traces": [{}],
    }))

    result = local_traces.scan(codex_dir=codex, claude_dir=claude, output=output)

    assert result["parsed_files"] == 1
    assert json.loads(output.read_text())["traces"] == []


def test_scan_and_summary_filter_by_project_harness_and_date(tmp_path):
    store = tmp_path / "local_traces.json"
    payload = {
        "schema_version": local_traces.SCHEMA,
        "generated_at": 1,
        "traces": [
            {"id": "one", "timestamp": "2026-07-27T10:00:00Z", "project": "alpha",
             "harness": "codex", "task": "one", "answer": "a", "skills": [], "usage": {}},
            {"id": "two", "timestamp": "2026-07-28T10:00:00Z", "project": "beta",
             "harness": "claude", "task": "two", "answer": "b", "skills": [], "usage": {}},
            {"id": "three", "timestamp": "2026-07-29T10:00:00Z", "project": "alpha",
             "harness": "claude", "task": "three", "answer": "c", "skills": [], "usage": {}},
        ],
    }
    store.write_text(json.dumps(payload))

    summary = local_traces.store_summary(
        store, project="alpha", harness="claude", since="2026-07-28", until="2026-07-29",
        include_tasks=True)

    assert summary["total"] == 1
    assert summary["recent"][0]["task"] == "three"
    assert summary["available_total"] == 3
    assert summary["available_projects"] == {"alpha": 2, "beta": 1}
    assert summary["available_harnesses"] == {"claude": 2, "codex": 1}
    with pytest.raises(ValueError, match="ISO date"):
        local_traces.store_summary(store, since="yesterday")

    store.write_text("{broken")
    assert local_traces.store_summary(store)["status"] == "unreadable"


def test_store_summary_filters_turns_by_observed_skill(tmp_path):
    """The traces page lists which skills were observed but could not narrow to one, so 'where was
    build-loop actually used' meant reading 4,639 turns by eye."""
    store = tmp_path / "store.json"
    store.write_text(json.dumps({
        "schema_version": local_traces.SCHEMA,
        "generated_at": "2026-08-07T00:00:00Z",
        "traces": [
            {"id": "a", "timestamp": "2026-08-01T10:00:00Z", "project": "p", "harness": "claude",
             "task": "a", "answer": "a", "usage": {},
             "skills": [{"name": "build-loop"}, {"name": "sota-check"}]},
            {"id": "b", "timestamp": "2026-08-02T10:00:00Z", "project": "p", "harness": "codex",
             "task": "b", "answer": "b", "usage": {}, "skills": [{"name": "sota-check"}]},
            {"id": "c", "timestamp": "2026-08-03T10:00:00Z", "project": "p", "harness": "claude",
             "task": "c", "answer": "c", "usage": {}, "skills": []},
        ],
    }))

    summary = local_traces.store_summary(store, skill="build-loop")
    assert summary["total"] == 1
    assert summary["recent"][0]["id"] == "a"
    assert summary["filters"]["skill"] == "build-loop"

    # Counted over every turn, not the filtered set: otherwise selecting a skill empties the
    # picker that selected it and you cannot switch to another.
    assert summary["available_skills"] == {"sota-check": 2, "build-loop": 1}
    assert local_traces.store_summary(store)["total"] == 3
