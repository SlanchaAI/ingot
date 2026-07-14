"""Unit tests for auto-drafted eval task sets (optimize.draft) — LLM mocked."""
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
