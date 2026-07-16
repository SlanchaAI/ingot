"""Unit tests for success/failure mining (optimize.mine) — Langfuse HTTP and the judge are mocked."""
import json

from optimize import mine
from optimize.judge import DIMENSIONS


class _Resp:
    def __init__(self, data):
        self._d = json.dumps({"data": data}).encode()

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_traces_keeps_only_usable_task_output_pairs(monkeypatch):
    data = [
        {"input": {"task": "do X", "rubric": "r"}, "output": "an answer", "tags": ["pdf"]},  # keep
        {"input": {"task": "do Y"}, "output": None, "tags": []},          # null output -> drop
        {"input": {"notask": "z"}, "output": "ans", "tags": []},          # no task -> drop
        {"input": {"task": "do Z"}, "output": "   ", "tags": []},         # blank output -> drop
        {"input": "a bare string", "output": "ans", "tags": []},          # non-dict input -> drop
    ]
    monkeypatch.setattr(mine.urllib.request, "urlopen", lambda req, timeout=60: _Resp(data))
    out = mine.fetch_traces(50)
    assert len(out) == 1 and out[0]["task"] == "do X" and out[0]["rubric"] == "r"


def test_fetch_traces_parses_langgraph_agent_traces(monkeypatch):
    lg = lambda task, answer: {"input": {"messages": [{"role": "user", "content": task}]},
                               "output": {"messages": [{"type": "ai", "content": answer}]}, "tags": ["demo"]}
    data = [
        lg("fill the form", "here is the code"),                                   # keep
        lg("rotate pages", ""),                                                    # empty answer -> drop
        {"input": {"messages": []}, "output": {"messages": []}, "tags": []},       # empty messages -> drop
        {"input": {"messages": [{"content": "blocks"}]},                           # content-block answer -> join
         "output": {"messages": [{"content": [{"type": "text", "text": "part1"},
                                              {"type": "text", "text": "part2"}]}]}, "tags": []},
    ]
    monkeypatch.setattr(mine.urllib.request, "urlopen", lambda req, timeout=60: _Resp(data))
    out = mine.fetch_traces(50)
    assert [t["task"] for t in out] == ["fill the form", "blocks"]
    assert out[0]["answer"] == "here is the code" and out[0]["rubric"] == ""
    assert out[1]["answer"] == "part1\npart2"


def test_mine_aggregates_failure_dimensions_and_mines_weakest(monkeypatch):
    traces = [{"task": f"t{i}", "rubric": "r", "answer": "a", "tags": []} for i in range(3)]
    monkeypatch.setattr(mine, "fetch_traces", lambda limit: traces)
    verdicts = iter([
        {"score": 0.1, "feedback": "f", "dimensions": {**{d: "pass" for d in DIMENSIONS}, "correctness": "wrong API"}},
        {"score": 0.2, "feedback": "f", "dimensions": {**{d: "pass" for d in DIMENSIONS}, "correctness": "also wrong"}},
        {"score": 0.9, "feedback": "f", "dimensions": {d: "pass" for d in DIMENSIONS}},
    ])
    monkeypatch.setattr(mine, "judge", lambda task, rubric, answer: next(verdicts))
    r = mine.mine("pdf", log=lambda *a, **k: None)
    assert r["traces"] == 3
    assert r["failure_dimensions"].get("correctness") == 2          # two traces failed correctness
    assert abs(r["mean_score"] - (0.1 + 0.2 + 0.9) / 3) < 1e-9
    assert r["mined_tasks"][0]["task"] in {"t0", "t1"}              # weakest first
