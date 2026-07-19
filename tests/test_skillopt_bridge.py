"""Unit tests for the SkillOpt bridge (no network/LLM): prompt loading, edit parsing, the pure
patch application + gate metric imported from the pinned package, and the prompt-glue helpers
driven by a stub reflection LM."""
import json

from optimize import skillopt_bridge as sk


def _lm(reply: str):
    """A fake reflection LM that returns a fixed reply regardless of the messages it is handed."""
    return lambda messages: reply


def test_all_vendored_prompts_load():
    for name in ("analyst_error", "ranking", "lr_autonomous", "slow_update"):
        assert len(sk.load_prompt(name)) > 50


def test_edits_from_reply_accepts_both_shapes_and_drops_malformed():
    nested = {"patch": {"edits": [
        {"op": "append", "content": "add me"},
        {"op": "replace", "target": "old", "content": "new"},
        {"op": "delete", "target": "gone"},
        {"op": "append", "content": ""},            # dropped: empty content
        {"op": "replace", "target": "", "content": "x"},  # dropped: no target
        {"op": "frobnicate", "content": "x"},       # dropped: unknown op
    ]}}
    edits = sk._edits_from_reply(nested)
    assert [e["op"] for e in edits] == ["append", "replace", "delete"]
    # bare {"edits": [...]} shape also works
    assert sk._edits_from_reply({"edits": [{"op": "append", "content": "y"}]})[0]["content"] == "y"
    assert sk._edits_from_reply("not a dict") == []


def test_apply_edits_uses_skillopt_patch_application():
    doc = "# Skill\nrule one"
    new, report = sk.apply_edits(doc, [{"op": "append", "content": "rule two", "target": ""},
                                       {"op": "replace", "target": "rule one", "content": "RULE ONE"}])
    assert "RULE ONE" in new and "rule two" in new
    assert [r["status"] for r in report] == ["applied_append", "applied_replace"]


def test_score_projects_onto_configured_metric(monkeypatch):
    monkeypatch.setenv("SKILLOPT_GATE_METRIC", "mixed")
    monkeypatch.setenv("SKILLOPT_GATE_MIXED_WEIGHT", "0.5")
    assert abs(sk.score(0.4, 0.8) - 0.6) < 1e-9        # (1-w)*hard + w*soft
    monkeypatch.setenv("SKILLOPT_GATE_METRIC", "hard")
    assert sk.score(0.4, 0.8) == 0.4
    monkeypatch.setenv("SKILLOPT_GATE_METRIC", "soft")
    assert sk.score(0.4, 0.8) == 0.8


def test_reflect_edits_parses_analyst_reply():
    reply = json.dumps({"failure_summary": [{"failure_type": "t", "count": 2, "description": "d"}],
                        "patch": {"edits": [{"op": "append", "content": "new rule"}]}})
    edits, summary = sk.reflect_edits("SKILL", [{"task": "t", "feedback": "f"}], "", 3, _lm(reply))
    assert edits == [{"op": "append", "content": "new rule", "target": ""}]
    assert summary[0]["description"] == "d"


def test_rank_edits_selects_indices_and_falls_back():
    edits = [{"op": "append", "content": f"e{i}", "target": ""} for i in range(4)]
    picked = sk.rank_edits("SKILL", edits, 2, _lm(json.dumps({"selected_indices": [3, 1]})))
    assert picked == [edits[3], edits[1]]
    # unusable ranking reply -> first `budget` edits
    assert sk.rank_edits("SKILL", edits, 2, _lm("garbage")) == edits[:2]
    # within budget -> no LLM call, returned unchanged
    assert sk.rank_edits("SKILL", edits[:2], 2, _lm("unused")) == edits[:2]


def test_decide_edit_budget_clamps_and_falls_back():
    edits = [{"op": "append", "content": f"e{i}", "target": ""} for i in range(4)]
    assert sk.decide_edit_budget("S", edits, 0.0, 0.0, 3, "", _lm(json.dumps({"learning_rate": 2})), 3) == 2
    # LR above the ceiling is clamped to min(ceiling, pool)
    assert sk.decide_edit_budget("S", edits, 0.0, 0.0, 3, "", _lm(json.dumps({"learning_rate": 99})), 3) == 3
    # unparseable -> ceiling (still makes progress); single-edit pool -> no LLM call
    assert sk.decide_edit_budget("S", edits, 0.0, 0.0, 3, "", _lm("nope"), 3) == 3
    assert sk.decide_edit_budget("S", edits[:1], 0.0, 0.0, 3, "", _lm("unused"), 3) == 1


def test_slow_update_returns_guidance_or_empty():
    assert sk.slow_update("A", "B", "compare", _lm(json.dumps({"guidance": "do X"}))) == "do X"
    assert sk.slow_update("A", "B", "compare", _lm("not json")) == ""
