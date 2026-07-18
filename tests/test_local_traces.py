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
    assert [t["task"] for t in traces] == ["t2", "t4"]   # last 3, minus the empty answer


def test_missing_local_store_explains_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACES_FILE", str(tmp_path / "absent.jsonl"))
    _kill_langfuse(monkeypatch)
    with pytest.raises(SystemExit, match="no local trace store"):
        mine_mod.fetch_traces(limit=10)
