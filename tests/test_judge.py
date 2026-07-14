"""Unit tests for the LLM judge's pure parsing/aggregation logic (LLM calls mocked)."""
import pytest

from optimize import judge as J
from optimize.judge import DIMENSIONS, _extract_json, failed_dimensions


class _FakeMsg:
    def __init__(self, content):
        self.content = content
        self.usage_metadata = None


def _mock_single_judge(monkeypatch, content):
    """Make the single-model judge return `content` from its one LLM call."""
    monkeypatch.setattr(J, "MODELS", ["mock-judge"])
    monkeypatch.setattr(J, "_get_llm", lambda model: type("L", (), {"invoke": lambda self, m: _FakeMsg(content)})())


# --- _extract_json: robust to prose / fences / stray braces -----------------------------------

def test_extract_json_plain():
    assert _extract_json('{"score": 0.9, "feedback": "x"}')["score"] == 0.9


def test_extract_json_with_surrounding_prose():
    assert _extract_json('Here is my grade: {"score": 0.8} — done.')["score"] == 0.8


def test_extract_json_inside_code_fence():
    assert _extract_json('```json\n{"score": 0.7}\n```')["score"] == 0.7


def test_extract_json_skips_non_score_braces():
    # the first {...} is not the score object; the extractor must skip it and find the real one
    assert _extract_json('The set {a, b} is covered. {"score": 0.5, "feedback": "ok"}')["score"] == 0.5


@pytest.mark.parametrize("text", ["no json here", "{not: valid}", '{"feedback": "no score key"}'])
def test_extract_json_returns_empty_when_no_score_object(text):
    assert _extract_json(text) == {}


# --- judge(): score clamping, unparseable fallback, dimension defaulting -----------------------

def test_judge_clamps_score_above_one(monkeypatch):
    _mock_single_judge(monkeypatch, '{"score": 1.7, "feedback": "great", "dimensions": {}}')
    assert J.judge("t", "r", "a")["score"] == 1.0


def test_judge_clamps_negative_score(monkeypatch):
    _mock_single_judge(monkeypatch, '{"score": -0.4, "feedback": "bad", "dimensions": {}}')
    assert J.judge("t", "r", "a")["score"] == 0.0


def test_judge_defaults_missing_dimensions_to_pass(monkeypatch):
    _mock_single_judge(monkeypatch, '{"score": 0.5, "dimensions": {"correctness": "wrong API"}}')
    r = J.judge("t", "r", "a")
    assert failed_dimensions(r["dimensions"]) == ["correctness"]      # only the one provided fails
    assert set(r["dimensions"]) == set(DIMENSIONS)                    # the rest are filled in as pass


def test_judge_unparseable_output_scores_zero(monkeypatch):
    _mock_single_judge(monkeypatch, "the model rambled and produced no JSON at all")
    r = J.judge("t", "r", "a")
    assert r["score"] == 0.0 and "unparseable" in r["feedback"]
    assert failed_dimensions(r["dimensions"]) == []                  # a parse failure isn't a skill failure


# --- failed_dimensions: case / synonyms ------------------------------------------------------

def test_failed_dimensions_treats_pass_synonyms_case_insensitively():
    dims = {"correctness": "PASS", "completeness": "ok", "instruction_following": "N/A", "efficiency": "  "}
    assert failed_dimensions(dims) == []
