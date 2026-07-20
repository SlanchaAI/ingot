"""Unit tests for auto-drafted eval task sets (optimize.draft), LLM mocked."""
import json

import pytest

from optimize import draft as D


class _FakeMsg:
    def __init__(self, content):
        self.content = content
        self.usage_metadata = None


def _mock_llm(monkeypatch, tasks):
    payload = json.dumps({"tasks": tasks})
    monkeypatch.setattr(D, "_llm", lambda: type("L", (), {"invoke": lambda self, p: _FakeMsg(payload)})())


def test_draft_splits_evenly_into_train_and_holdout(monkeypatch):
    tasks = [{"task": f"task {i}", "rubric": f"rubric {i}"} for i in range(8)]
    _mock_llm(monkeypatch, tasks)
    out = D.draft_tasks("pdf", "desc", "body", n=8)
    assert len(out["train"]) == 4 and len(out["holdout"]) == 4
    train_texts = {t["task"] for t in out["train"]}
    holdout_texts = {t["task"] for t in out["holdout"]}
    assert train_texts.isdisjoint(holdout_texts)                 # no leakage between splits


def test_draft_drops_malformed_task_entries(monkeypatch):
    tasks = [{"task": "ok1", "rubric": "r"}, {"nope": "x"}, {"task": "ok2"}, {"task": "ok3"}, {"task": "ok4"}]
    _mock_llm(monkeypatch, tasks)
    out = D.draft_tasks("pdf", "d", "b", n=5)
    all_tasks = out["train"] + out["holdout"]
    assert all(t.get("task") for t in all_tasks) and len(all_tasks) == 4  # the entry without a task is dropped


def test_draft_raises_if_too_few_usable_tasks(monkeypatch):
    _mock_llm(monkeypatch, [{"task": "only one", "rubric": "r"}])
    with pytest.raises(SystemExit):
        D.draft_tasks("pdf", "d", "b", n=8)


def _mock_routing_llm(monkeypatch, positive, negative):
    payload = json.dumps({"positive": positive, "negative": negative})
    monkeypatch.setattr(D, "_llm", lambda: type("L", (), {"invoke": lambda self, p: _FakeMsg(payload)})())


def test_draft_routing_cases_shape(monkeypatch):
    _mock_routing_llm(monkeypatch, ["sum a column", "lookup a value", "fix my formula", "date math"],
                      ["convert xlsx to csv", "thanks!"])
    cases = D.draft_routing_cases("excel-formulas", "desc", "body")
    positives = [c for c in cases if c["expected"] == "excel-formulas"]
    negatives = [c for c in cases if c["expected"] is None]
    assert len(positives) == 4 and len(negatives) == 2
    assert positives[0]["parity"] is True and negatives[0]["parity"] is True
    assert positives[1]["harness"] == "claude"            # cross-harness coverage
    assert all("parity" not in c for c in positives[2:])


def test_draft_routing_cases_rejects_thin_output(monkeypatch):
    _mock_routing_llm(monkeypatch, ["only one"], [])
    with pytest.raises(SystemExit, match="need at least"):
        D.draft_routing_cases("excel-formulas", "desc", "body")


def test_draft_and_append_routing_preserves_existing_tasks(monkeypatch, tmp_path):
    import yaml
    _mock_routing_llm(monkeypatch, ["a", "b"], ["c"])
    (tmp_path / "sk.yaml").write_text("skill: sk\ntrain:\n- task: t\n  rubric: r\n")
    D.draft_and_append_routing("sk", "desc", "body", tmp_path)
    data = yaml.safe_load((tmp_path / "sk.yaml").read_text())
    assert data["train"] == [{"task": "t", "rubric": "r"}]        # untouched
    assert len(data["routing"]) == 3 and data["routing"][2]["expected"] is None
