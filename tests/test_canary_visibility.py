"""Canary A/B visibility in Langfuse: pinned trace ids, arm/revision tags, outcome score
write-back. Langfuse and the agent are stubbed — no network."""
import asyncio

import pytest

import agent.run as agent_run
from optimize import canary as C


class _FakeLangfuse:
    def __init__(self):
        self.scores = []
        self.flushed = 0
        self._n = 0

    def create_trace_id(self):
        self._n += 1
        return f"trace-{self._n}"

    def create_score(self, **kw):
        self.scores.append(kw)

    def flush(self):
        self.flushed += 1


def test_record_outcome_writes_judge_and_success_scores():
    lf = _FakeLangfuse()
    C.record_outcome(lf, "trace-7", {"score": 0.8, "feedback": "solid"}, True)
    assert [s["name"] for s in lf.scores] == ["canary_judge", "canary_success"]
    judge_score = lf.scores[0]
    assert judge_score == {"trace_id": "trace-7", "name": "canary_judge", "value": 0.8,
                           "comment": "solid"}
    assert lf.scores[1]["value"] == 1.0


def test_record_outcome_is_a_noop_without_langfuse_or_trace():
    lf = _FakeLangfuse()
    C.record_outcome(None, "trace-1", {"score": 1.0}, True)
    C.record_outcome(lf, None, {"score": 1.0}, True)
    assert lf.scores == []


def test_record_outcome_truncates_long_feedback():
    lf = _FakeLangfuse()
    C.record_outcome(lf, "t", {"score": 0.1, "feedback": "x" * 2000}, False)
    assert len(lf.scores[0]["comment"]) == 500


def test_langfuse_config_empty_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert agent_run.langfuse_config(tags=["x"], trace_id="t") == {}


def test_langfuse_config_pins_trace_id(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    captured = {}

    class FakeHandler:
        def __init__(self, trace_context=None):
            captured["trace_context"] = trace_context
    import langfuse.langchain
    monkeypatch.setattr(langfuse.langchain, "CallbackHandler", FakeHandler)
    cfg = agent_run.langfuse_config(tags=["canary=champion"], trace_id="trace-42")
    assert captured["trace_context"] == {"trace_id": "trace-42"}
    assert cfg["metadata"]["langfuse_tags"] == ["canary=champion"]
    cfg = agent_run.langfuse_config()
    assert captured["trace_context"] is None  # no pin unless asked


def test_canary_loop_tags_arms_with_revisions(monkeypatch, tmp_path):
    """One full (stubbed) canary request cycle: the trace id is pinned, tags carry the arm and
    exact revision, and outcome scores land on the same trace."""
    skill = tmp_path / "skills" / "pdf"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: pdf\ndescription: Merge PDFs.\n---\nbody\n")
    monkeypatch.setattr(C, "SKILLS_DIR", tmp_path / "skills")

    lf = _FakeLangfuse()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    import langfuse
    monkeypatch.setattr(langfuse, "get_client", lambda: lf)

    seen = {"tags": [], "trace_ids": []}

    def fake_config(tags=None, trace_id=None):
        if tags is not None:
            seen["tags"].append(tags)
            seen["trace_ids"].append(trace_id)
        return {"configured": True}
    monkeypatch.setattr(C, "langfuse_config", fake_config)
    monkeypatch.setattr(C, "build_agent", lambda tools, instructions=None: "agent")
    monkeypatch.setattr(C, "_variant_tools", lambda *a, **k: [])

    async def fake_run(agent, task, config=None):
        return "an answer", [], {}
    monkeypatch.setattr(C, "run_task", fake_run)
    monkeypatch.setattr(C, "judge", lambda t, r, a, check=None: {"score": 1.0, "feedback": "good"})
    monkeypatch.setattr(C, "load_tasks", lambda s: ([{"task": "t", "rubric": "r"}], [], {}))
    monkeypatch.setattr(C, "load_pending", lambda s: {"challenger_components":
                                                      {"description": "Merge PDFs.", "body": "new"}})

    result = C.run_canary("pdf", epsilon=0.0, min_samples=1, max_requests=2, seed=0)

    assert seen["trace_ids"] == ["trace-1", "trace-2"]
    assert all(t[0] == "canary=champion" for t in seen["tags"])          # epsilon 0 -> champion arm
    assert all(t[1].startswith("revision=") for t in seen["tags"])
    assert {s["trace_id"] for s in lf.scores} == {"trace-1", "trace-2"}
    assert lf.flushed >= 1
    assert result["decision"] in ("inconclusive", "reject", "promote")
