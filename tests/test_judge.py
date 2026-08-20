"""Unit tests for the LLM judge's pure parsing/aggregation logic (LLM calls mocked)."""
import json

import pytest

from ingot.optimize import agy_judge as A
from ingot.optimize import judge as J
from ingot.optimize.judge import (DEFAULT_CHECKLIST, DIMENSIONS, _extract_json, _weighted,
                                  failed_dimensions)


class _FakeMsg:
    def __init__(self, content):
        self.content = content
        self.usage_metadata = None


def _mock_single_judge(monkeypatch, content):
    """Make the single-model judge return `content` from its one LLM call."""
    monkeypatch.setattr(J, "MODELS", ["mock-judge"])
    monkeypatch.setattr(J, "_get_llm", lambda model: type("L", (), {"invoke": lambda self, m: _FakeMsg(content)})())


def _mock_judges(monkeypatch, contents):
    """One scripted response per model, in order, for ensemble tests."""
    monkeypatch.setattr(J, "MODELS", [f"mock-{i}" for i in range(len(contents))])
    by_model = {f"mock-{i}": c for i, c in enumerate(contents)}
    monkeypatch.setattr(J, "_get_llm", lambda model: type(
        "L", (), {"invoke": lambda self, m, _c=by_model[model]: _FakeMsg(_c)})())


def _items(**verdicts) -> str:
    return json.dumps({"items": {k: {"verdict": v, "note": "n"} for k, v in verdicts.items()},
                       "feedback": "f"})


def _all(verdict: str) -> str:
    return _items(**{i["id"]: verdict for i in DEFAULT_CHECKLIST})


def _agy_grade(verdict: str = "pass") -> dict:
    return {
        "items": {
            item["id"]: {"verdict": verdict, "note": ""}
            for item in DEFAULT_CHECKLIST
        },
        "feedback": "agy feedback",
    }


# --- backend selection -----------------------------------------------------------------------

def test_agy_backend_uses_one_subscription_grade_without_openrouter(monkeypatch):
    monkeypatch.setenv("JUDGE_BACKEND", "agy")
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-be-used")
    monkeypatch.setattr(J, "MODELS", ["openrouter/one", "openrouter/two"])
    monkeypatch.setattr(J, "_get_llm", lambda *_args: pytest.fail("OpenRouter fallback ran"))
    usage = {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
    calls = []

    def invoke(prompt, checklist):
        calls.append((prompt, checklist))
        return _agy_grade(), usage

    ledger = []
    monkeypatch.setattr(A, "invoke", invoke)
    monkeypatch.setattr(
        J.usage_ledger,
        "add",
        lambda role, observed, **metadata: ledger.append((role, observed, metadata)),
    )

    result = J.judge("task", answer="answer")

    assert result["score"] == 1.0
    assert result["feedback"] == "agy feedback"
    assert len(calls) == 1
    assert ledger == [("judge", usage, {"billing_mode": "subscription"})]


def test_agy_backend_error_propagates_without_openrouter_fallback(monkeypatch):
    monkeypatch.setenv("JUDGE_BACKEND", "agy")
    monkeypatch.setenv("OPENROUTER_PROVIDERS", "fallback-provider")
    monkeypatch.setattr(J, "_get_llm", lambda *_args: pytest.fail("OpenRouter fallback ran"))
    monkeypatch.setattr(
        A,
        "invoke",
        lambda *_args: (_ for _ in ()).throw(A.AgyJudgeError("agy unavailable")),
    )

    with pytest.raises(A.AgyJudgeError, match="agy unavailable"):
        J.judge("task", answer="answer")


# --- _extract_json: robust to prose / fences / stray braces -----------------------------------

def test_extract_json_plain():
    assert _extract_json('{"score": 0.9, "feedback": "x"}')["score"] == 0.9


def test_extract_json_with_surrounding_prose():
    assert _extract_json('Here is my grade: {"score": 0.8}, done.')["score"] == 0.8


def test_extract_json_inside_code_fence():
    assert _extract_json('```json\n{"score": 0.7}\n```')["score"] == 0.7


def test_extract_json_skips_non_score_braces():
    # the first {...} is not the score object; the extractor must skip it and find the real one
    assert _extract_json('The set {a, b} is covered. {"score": 0.5, "feedback": "ok"}')["score"] == 0.5


@pytest.mark.parametrize("text", ["no json here", "{not: valid}", '{"feedback": "no score key"}'])
def test_extract_json_returns_empty_when_no_score_object(text):
    assert _extract_json(text) == {}


def test_extract_json_finds_the_requested_key():
    """The judge asks for `items`; the score key it used to look for no longer appears."""
    assert _extract_json('{"items": {"a": "pass"}}', "items")["items"] == {"a": "pass"}


# --- the score is derived, never taken from the model -----------------------------------------

def test_score_is_the_weighted_mean_of_the_checklist(monkeypatch):
    """DEFAULT_CHECKLIST weights are 3/2/2/1. Failing only `efficiency` (weight 1) must cost
    exactly 1/8 of the score, not whatever the model felt like reporting."""
    _mock_single_judge(monkeypatch, _items(correctness="pass", completeness="pass",
                                           instruction_following="pass", efficiency="fail"))
    assert J.judge("t", "r", "a")["score"] == pytest.approx(7 / 8)


def test_a_model_supplied_score_is_ignored(monkeypatch):
    """The old contract let the judge name its own number. A model that still emits one must not
    be able to override the checklist -- that is the entire point of grading against items."""
    payload = json.loads(_all("fail"))
    payload["score"] = 1.0
    _mock_single_judge(monkeypatch, json.dumps(payload))
    assert J.judge("t", "r", "a")["score"] == 0.0


def test_partial_verdicts_score_half(monkeypatch):
    _mock_single_judge(monkeypatch, _all("partial"))
    assert J.judge("t", "r", "a")["score"] == pytest.approx(0.5)


def test_score_cannot_leave_zero_to_one(monkeypatch):
    """Clamping used to be needed because the model picked the number. A weighted mean of values
    in [0,1] cannot leave the range, so the property holds by construction."""
    for verdict in ("pass", "partial", "fail"):
        _mock_single_judge(monkeypatch, _all(verdict))
        assert 0.0 <= J.judge("t", "r", "a")["score"] <= 1.0


# --- items the judge did not answer, or answered badly, are not passes ------------------------

def test_an_ungraded_item_is_not_a_pass(monkeypatch):
    """Omitting an item must cost its weight. Defaulting it to pass would let a lazy judge score
    1.0 by answering one item."""
    _mock_single_judge(monkeypatch, _items(correctness="pass"))
    r = J.judge("t", "r", "a")
    assert r["score"] == pytest.approx(3 / 8)
    assert r["checklist"]["efficiency"]["note"] == "not graded"


def test_an_unrecognized_verdict_fails_rather_than_passes(monkeypatch):
    _mock_single_judge(monkeypatch, _items(correctness="excellent", completeness="pass",
                                           instruction_following="pass", efficiency="pass"))
    r = J.judge("t", "r", "a")
    assert r["score"] == pytest.approx(5 / 8)          # correctness carries weight 3 of 8


def test_an_unrecognized_verdict_without_a_note_says_it_was_ungraded(monkeypatch):
    """The model's own note is kept when it wrote one; the fallback only fills a silent item so
    the reviewer never sees a bare zero with no reason."""
    _mock_single_judge(monkeypatch, json.dumps({"items": {"correctness": "excellent"},
                                                "feedback": "f"}))
    assert "ungraded" in J.judge("t", "r", "a")["checklist"]["correctness"]["note"]


def test_unparseable_output_scores_zero_without_blaming_the_skill(monkeypatch):
    _mock_single_judge(monkeypatch, "the model rambled and produced no JSON at all")
    r = J.judge("t", "r", "a")
    assert r["score"] == 0.0 and "unparseable" in r["feedback"]
    assert failed_dimensions(r["dimensions"]) == []      # a parse failure isn't a skill failure


# --- custom checklists (the Tessl-style graded rubric) ----------------------------------------

CUSTOM = [
    {"id": "cites_source", "dimension": "correctness", "weight": 4,
     "criterion": "Every factual claim names its source."},
    {"id": "states_tradeoff", "dimension": "completeness", "weight": 1,
     "criterion": "It names at least one tradeoff."},
]


def test_a_task_checklist_replaces_the_default(monkeypatch):
    _mock_single_judge(monkeypatch, _items(cites_source="pass", states_tradeoff="fail"))
    r = J.judge("t", "r", "a", checklist=CUSTOM)
    assert r["score"] == pytest.approx(4 / 5)
    assert set(r["checklist"]) == {"cites_source", "states_tradeoff"}


def test_a_malformed_checklist_falls_back_to_the_default(monkeypatch):
    """Items without an id or criterion cannot be graded; an empty result must not mean 'no checks
    ran, therefore full marks'."""
    _mock_single_judge(monkeypatch, _all("pass"))
    r = J.judge("t", "r", "a", checklist=[{"weight": 9}, {"id": "x"}])
    assert set(r["checklist"]) == {i["id"] for i in DEFAULT_CHECKLIST}


def test_custom_items_map_onto_the_reported_dimensions(monkeypatch):
    """mine.py and the candidate search consume `dimensions`, so a custom rubric still has to
    report through them."""
    _mock_single_judge(monkeypatch, _items(cites_source="fail", states_tradeoff="pass"))
    r = J.judge("t", "r", "a", checklist=CUSTOM)
    assert failed_dimensions(r["dimensions"]) == ["correctness"]


# --- resolution: the reason for the whole change ----------------------------------------------

def test_the_checklist_resolves_finer_than_a_holistic_ladder():
    """A judge naming one number emits a coarse ladder (0.9 / 0.95 / 1.0), so a mean can move a
    whole rung because two tasks crossed a boundary. Independent weighted items give many more
    reachable values, which is what makes a small real effect distinguishable from noise."""
    weights = [i["weight"] for i in DEFAULT_CHECKLIST]
    reachable = set()
    for bits in range(3 ** len(weights)):
        values, b = [], bits
        for _ in weights:
            values.append([0.0, 0.5, 1.0][b % 3]); b //= 3
        graded = {i["id"]: {"value": v} for i, v in zip(DEFAULT_CHECKLIST, values)}
        reachable.add(round(_weighted(graded, DEFAULT_CHECKLIST), 6))
    assert len(reachable) >= 17          # vs the 3 rungs a holistic judge actually used


# --- ensembles average per item, not per answer -----------------------------------------------

def test_ensemble_averages_each_item_before_weighting(monkeypatch):
    """Averaging item values is smoother than averaging whole-answer scores: two judges splitting
    on one item move the result by half that item's weight, not by half the answer."""
    _mock_judges(monkeypatch, [_all("pass"),
                               _items(correctness="pass", completeness="pass",
                                      instruction_following="pass", efficiency="fail")])
    r = J.judge("t", "r", "a")
    assert r["checklist"]["efficiency"]["value"] == pytest.approx(0.5)
    assert r["score"] == pytest.approx(1 - 0.5 * (1 / 8))


def test_a_minority_failure_does_not_fail_the_dimension(monkeypatch):
    """One judge of three flagging an item leaves its value at 2/3, above the failure threshold,
    so the dimension still reads as a pass."""
    _mock_judges(monkeypatch, [_all("pass"), _all("pass"),
                               _items(correctness="fail", completeness="pass",
                                      instruction_following="pass", efficiency="pass")])
    assert failed_dimensions(J.judge("t", "r", "a")["dimensions"]) == []


def test_one_unparseable_judge_does_not_sink_the_ensemble(monkeypatch):
    _mock_judges(monkeypatch, [_all("pass"), "no json here"])
    assert J.judge("t", "r", "a")["score"] == pytest.approx(1.0)


# --- failed_dimensions: case / synonyms ------------------------------------------------------

def test_failed_dimensions_treats_pass_synonyms_case_insensitively():
    dims = {"correctness": "PASS", "completeness": "ok", "instruction_following": "N/A", "efficiency": "  "}
    assert failed_dimensions(dims) == []
