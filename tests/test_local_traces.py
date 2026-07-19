"""The local JSONL trace store: the agent's writer and mine's Langfuse-unreachable fallback
must agree on shape, so the learn-from-traffic loop works without the tracing stack."""
import json
import urllib.request

import pytest

from agent.run import _log_local_trace
from optimize import mine as mine_mod


def _kill_langfuse(monkeypatch):
    def refused(url, timeout=60):
        raise OSError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", refused)


def test_agent_writes_what_mine_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACES_FILE", str(tmp_path / "traces.jsonl"))
    _kill_langfuse(monkeypatch)
    _log_local_trace("merge two PDFs", "use pypdf", ["demo", "pdf", "revision=pdf@abc123"])
    traces = mine_mod.fetch_traces(limit=50)
    assert traces == [{"task": "merge two PDFs", "rubric": "", "answer": "use pypdf",
                       "tags": ["demo", "pdf", "revision=pdf@abc123"]}]


def test_local_fallback_honors_limit_and_skips_empty_answers(tmp_path, monkeypatch):
    p = tmp_path / "traces.jsonl"
    monkeypatch.setenv("TRACES_FILE", str(p))
    _kill_langfuse(monkeypatch)
    records = [{"task": f"t{i}", "answer": f"a{i}", "tags": []} for i in range(5)]
    records[3]["answer"] = ""                       # judged nothing: not minable
    p.write_text("\n".join(json.dumps(r) for r in records))
    traces = mine_mod.fetch_traces(limit=3)
    assert [t["task"] for t in traces] == ["t1", "t2", "t4"]


def test_local_fallback_reads_backups_oldest_to_active_before_limiting(tmp_path, monkeypatch):
    path = tmp_path / "traces.jsonl"
    monkeypatch.setenv("TRACES_FILE", str(path))
    monkeypatch.setenv("LOCAL_TRACE_BACKUPS", "2")
    _kill_langfuse(monkeypatch)
    path.with_name("traces.jsonl.2").write_text(json.dumps({"task": "old", "answer": "a"}))
    path.with_name("traces.jsonl.1").write_text("bad-json\n" + json.dumps(
        {"task": "middle", "answer": "b"}))
    path.write_text("[]\n" + json.dumps({"task": "new", "answer": "c"}))

    traces = mine_mod.fetch_traces(limit=2)

    assert [trace["task"] for trace in traces] == ["middle", "new"]


def test_missing_local_store_explains_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACES_FILE", str(tmp_path / "absent.jsonl"))
    _kill_langfuse(monkeypatch)
    with pytest.raises(SystemExit, match="no local trace store"):
        mine_mod.fetch_traces(limit=10)


def test_trace_opt_out_redaction_schema_permissions_and_rotation(tmp_path, monkeypatch):
    path = tmp_path / "private" / "traces.jsonl"
    monkeypatch.setenv("TRACES_FILE", str(path))
    monkeypatch.setenv("LOCAL_TRACE_ENABLED", "false")
    _log_local_trace("task", "answer", [])
    assert not path.exists()

    monkeypatch.setenv("LOCAL_TRACE_ENABLED", "true")
    monkeypatch.setenv("LOCAL_TRACE_MAX_BYTES", "1")
    monkeypatch.setenv("LOCAL_TRACE_BACKUPS", "1")
    _log_local_trace("token=top-secret", "password=hunter2", ["demo"])
    first = json.loads(path.read_text())
    assert first["schema_version"] == 1
    assert "top-secret" not in str(first) and "hunter2" not in str(first)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    _log_local_trace("next", "answer", [])
    assert path.with_name("traces.jsonl.1").exists()


def test_local_reader_accepts_original_and_versioned_records_and_skips_bad_lines(
        tmp_path, monkeypatch):
    path = tmp_path / "traces.jsonl"
    monkeypatch.setenv("TRACES_FILE", str(path))
    _kill_langfuse(monkeypatch)
    path.write_text('\n'.join([
        json.dumps({"task": "old", "answer": "a", "tags": []}),
        "not-json",
        json.dumps({"schema_version": 1, "task": "new", "answer": "b", "tags": []}),
        json.dumps({"schema_version": 99, "task": "future", "answer": "c", "tags": []}),
    ]))
    assert [record["task"] for record in mine_mod.fetch_traces(10)] == ["old", "new"]


def test_trace_age_retention_prunes_expired_records(tmp_path, monkeypatch):
    path = tmp_path / "traces.jsonl"
    path.write_text(json.dumps({"ts": 1, "task": "expired", "answer": "a", "tags": []}) + "\n")
    monkeypatch.setenv("TRACES_FILE", str(path))
    monkeypatch.setenv("LOCAL_TRACE_MAX_AGE_DAYS", "1")
    _log_local_trace("current", "b", [])
    assert [json.loads(line)["task"] for line in path.read_text().splitlines()] == ["current"]


def test_trace_age_retention_prunes_backups_and_preserves_malformed_lines(tmp_path, monkeypatch):
    path = tmp_path / "traces.jsonl"
    backup = path.with_name("traces.jsonl.1")
    oldest_backup = path.with_name("traces.jsonl.2")
    backup.write_text("not-json\n[]\n" + json.dumps(
        {"ts": 1, "task": "expired secret", "answer": "sensitive"}) + "\n" + json.dumps(
        {"task": "undated", "answer": "preserved"}) + "\n")
    oldest_backup.write_text(json.dumps(
        {"ts": 1, "task": "older secret", "answer": "sensitive"}) + "\n")
    monkeypatch.setenv("TRACES_FILE", str(path))
    monkeypatch.setenv("LOCAL_TRACE_BACKUPS", "2")
    monkeypatch.setenv("LOCAL_TRACE_MAX_AGE_DAYS", "1")

    _log_local_trace("current", "answer", [])

    assert backup.read_text().splitlines() == [
        "not-json", "[]", json.dumps({"task": "undated", "answer": "preserved"})]
    assert not oldest_backup.exists()


def test_trace_retention_failure_does_not_break_serving(tmp_path, monkeypatch, capsys):
    path = tmp_path / "traces.jsonl"
    path.write_bytes(b"\xff")
    monkeypatch.setenv("TRACES_FILE", str(path))
    monkeypatch.setenv("LOCAL_TRACE_MAX_AGE_DAYS", "1")

    _log_local_trace("current", "answer", [])

    assert "local trace store unavailable" in capsys.readouterr().out


@pytest.mark.parametrize("malformed_record", [None, [], "text", 42, 3.5])
def test_trace_age_retention_preserves_non_object_json(tmp_path, monkeypatch, malformed_record):
    path = tmp_path / "traces.jsonl"
    original = json.dumps(malformed_record)
    path.write_text(original + "\n")
    monkeypatch.setenv("TRACES_FILE", str(path))
    monkeypatch.setenv("LOCAL_TRACE_MAX_AGE_DAYS", "1")

    _log_local_trace("current", "answer", [])

    lines = path.read_text().splitlines()
    assert lines[0] == original
    assert json.loads(lines[1])["task"] == "current"
