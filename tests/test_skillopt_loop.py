"""Unit tests for the SkillOpt body-pass loop (no network/LLM): rollouts and the reflection LM are
stubbed so the orchestration, accept on improvement, keep the seed otherwise, and buffer rejected
edits back into the next reflection, is exercised deterministically."""
import json

import pytest

from optimize import rollout as R
from optimize import skillopt_loop as S


def _fake_lm(reply_for):
    """Route each call to a canned reply by which vendored prompt is in the system message, and
    record every analyst (reflect) user-message + slow-update invocation for assertions."""
    seen = {"reflect_user_msgs": [], "slow_calls": 0}

    def lm(messages):
        system = messages[0]["content"]
        user = messages[1]["content"] if len(messages) > 1 else ""
        if "failure-analysis" in system:            # analyst_error.md
            seen["reflect_user_msgs"].append(user)
            return reply_for["analyst"]
        if "update-size controller" in system:      # lr_autonomous.md
            return reply_for.get("lr", json.dumps({"learning_rate": 1}))
        if "RANK" in system:                        # ranking.md
            return reply_for.get("ranking", json.dumps({"selected_indices": [0]}))
        seen["slow_calls"] += 1                      # slow_update.md
        return reply_for.get("slow", json.dumps({"guidance": "consolidated guidance"}))

    lm.seen = seen
    return lm


def _install(monkeypatch, lm, scorer, answer_fn=None):
    """Wire the loop to a stub reflection LM and a body-content-based rollout scorer. `answer_fn`
    optionally derives the rollout's answer text from the served system (for acceptance-criteria
    tests, which inspect the answer, not the score)."""
    monkeypatch.setattr(S, "make_reflection_lm", lambda: lm)

    def _rollout(self, system, ex):
        ans = answer_fn(system) if answer_fn else "ans"
        return ans, scorer(system), {"task": ex["task"], "feedback": "fb",
                                     "output": ans, "dimensions": {}}
    monkeypatch.setattr(R.SkillAdapter, "_rollout", _rollout)


TRAIN = [{"task": "t1", "rubric": "r"}, {"task": "t2", "rubric": "r"}, {"task": "t3", "rubric": "r"}]


def test_accepts_an_improving_edit(monkeypatch):
    monkeypatch.setenv("SKILLOPT_EPOCHS", "1")
    monkeypatch.setenv("SKILLOPT_MINIBATCH", "10")   # one minibatch
    lm = _fake_lm({"analyst": json.dumps(
        {"failure_summary": [{"description": "misses the rule"}],
         "patch": {"edits": [{"op": "append", "content": "GOOD"}]}})})
    _install(monkeypatch, lm, scorer=lambda system: 1.0 if "GOOD" in system else 0.0)
    best, seed_score, best_score = S.run_skillopt({"body": "SEED"}, TRAIN, log=lambda *a: None)
    assert "GOOD" in best["body"]
    assert seed_score == 0.0 and best_score == 1.0


def test_keeps_seed_when_no_edit_improves(monkeypatch):
    monkeypatch.setenv("SKILLOPT_EPOCHS", "1")
    monkeypatch.setenv("SKILLOPT_MINIBATCH", "10")
    lm = _fake_lm({"analyst": json.dumps(
        {"failure_summary": [], "patch": {"edits": [{"op": "append", "content": "NEUTRAL"}]}})})
    _install(monkeypatch, lm, scorer=lambda system: 0.0)   # nothing the optimizer does helps
    best, seed_score, best_score = S.run_skillopt({"body": "SEED"}, TRAIN, log=lambda *a: None)
    assert best == {"body": "SEED"}          # seed returned unchanged
    assert best_score == 0.0


def test_rejected_edit_is_buffered_into_next_reflection(monkeypatch):
    monkeypatch.setenv("SKILLOPT_EPOCHS", "1")
    monkeypatch.setenv("SKILLOPT_MINIBATCH", "1")     # -> 3 minibatches, so a later reflect exists
    lm = _fake_lm({"analyst": json.dumps(
        {"failure_summary": [], "patch": {"edits": [{"op": "append", "content": "NOPE"}]}})})
    _install(monkeypatch, lm, scorer=lambda system: 0.0)   # every candidate is rejected by the gate
    S.run_skillopt({"body": "SEED"}, TRAIN, log=lambda *a: None)
    # the first step's rejected edit must surface in a later reflection's step-buffer section
    assert any("REJECTED edit" in msg for msg in lm.seen["reflect_user_msgs"][1:])


def test_edits_over_budget_are_clipped(monkeypatch):
    monkeypatch.setenv("SKILLOPT_EPOCHS", "1")
    monkeypatch.setenv("SKILLOPT_MINIBATCH", "10")
    monkeypatch.setenv("SKILLOPT_MAX_EDITS", "1")     # budget 1, analyst proposes 2 -> ranking clips
    lm = _fake_lm({"analyst": json.dumps({"failure_summary": [], "patch": {"edits": [
                       {"op": "append", "content": "AAA"}, {"op": "append", "content": "BBB"}]}}),
                   "ranking": json.dumps({"selected_indices": [0]})})   # keep only the first
    _install(monkeypatch, lm, scorer=lambda system: 1.0 if "AAA" in system else 0.0)
    best, _, _ = S.run_skillopt({"body": "SEED"}, TRAIN, log=lambda *a: None)
    assert "AAA" in best["body"] and "BBB" not in best["body"]


def test_length_penalty_reduces_the_returned_score(monkeypatch):
    monkeypatch.setenv("SKILLOPT_EPOCHS", "1")
    monkeypatch.setenv("SKILLOPT_MINIBATCH", "10")
    monkeypatch.setattr(S, "length_penalty", lambda body: 0.2 if "BLOAT" in body else 0.0)
    lm = _fake_lm({"analyst": json.dumps(
        {"failure_summary": [], "patch": {"edits": [{"op": "append", "content": "BLOAT FIX"}]}})})
    _install(monkeypatch, lm, scorer=lambda system: 1.0 if "BLOAT" in system else 0.0)
    best, _, best_score = S.run_skillopt({"body": "SEED"}, TRAIN, log=lambda *a: None)
    assert "BLOAT" in best["body"]
    assert best_score == pytest.approx(0.8)          # judge 1.0 minus the 0.2 length penalty


def test_frozen_components_are_rendered_into_rollouts(monkeypatch):
    monkeypatch.setenv("SKILLOPT_EPOCHS", "1")
    monkeypatch.setenv("SKILLOPT_MINIBATCH", "10")
    seen = {}
    lm = _fake_lm({"analyst": json.dumps(
        {"failure_summary": [], "patch": {"edits": [{"op": "append", "content": "FIX"}]}})})

    def scorer(system):
        seen["had_frozen"] = "TRIGGER-DESC" in system
        return 1.0 if "FIX" in system else 0.0
    _install(monkeypatch, lm, scorer=scorer)
    S.run_skillopt({"body": "SEED"}, TRAIN, frozen={"description": "TRIGGER-DESC"}, log=lambda *a: None)
    assert seen["had_frozen"]                          # adapter.serve() renders the frozen description


def test_second_epoch_runs_slow_update(monkeypatch):
    monkeypatch.setenv("SKILLOPT_EPOCHS", "2")
    monkeypatch.setenv("SKILLOPT_MINIBATCH", "10")
    lm = _fake_lm({"analyst": json.dumps(
        {"failure_summary": [], "patch": {"edits": [{"op": "append", "content": "GOOD"}]}})})
    _install(monkeypatch, lm, scorer=lambda system: 1.0 if "GOOD" in system else 0.0)
    S.run_skillopt({"body": "SEED"}, TRAIN, log=lambda *a: None)
    assert lm.seen["slow_calls"] >= 1                  # epoch-end consolidation fired after an accept


def test_empty_tasks_keeps_seed():
    best, seed_score, best_score = S.run_skillopt({"body": "SEED"}, [], log=lambda *a: None)
    assert best == {"body": "SEED"} and (seed_score, best_score) == (0.0, 0.0)


def test_zero_learning_rate_applies_no_edit(monkeypatch):
    monkeypatch.setenv("SKILLOPT_EPOCHS", "1")
    monkeypatch.setenv("SKILLOPT_MINIBATCH", "10")
    monkeypatch.setenv("SKILLOPT_MAX_EDITS", "3")     # >1 so the learning-rate controller is consulted
    lm = _fake_lm({"analyst": json.dumps({"failure_summary": [], "patch": {"edits": [
                       {"op": "append", "content": "X1"}, {"op": "append", "content": "X2"}]}}),
                   "lr": json.dumps({"learning_rate": 0})})   # apply nothing this step
    _install(monkeypatch, lm, scorer=lambda system: 1.0 if "X1" in system else 0.0)
    best, _, _ = S.run_skillopt({"body": "SEED"}, TRAIN, log=lambda *a: None)
    assert best == {"body": "SEED"}                   # LR 0 -> no edit applied, seed kept


def test_reflection_with_no_edits_keeps_seed(monkeypatch):
    monkeypatch.setenv("SKILLOPT_EPOCHS", "1")
    monkeypatch.setenv("SKILLOPT_MINIBATCH", "10")
    lm = _fake_lm({"analyst": json.dumps(
        {"failure_summary": [{"description": "d"}], "patch": {"edits": []}})})  # nothing proposed
    _install(monkeypatch, lm, scorer=lambda system: 0.0)   # failing, but reflection is empty
    best, _, _ = S.run_skillopt({"body": "SEED"}, TRAIN, log=lambda *a: None)
    assert best == {"body": "SEED"}


def test_acceptance_penalty_docks_a_violating_candidate(monkeypatch):
    import re
    monkeypatch.setenv("SKILLOPT_EPOCHS", "1")
    monkeypatch.setenv("SKILLOPT_MINIBATCH", "10")
    monkeypatch.setenv("SKILLOPT_ACCEPT_PENALTY", "0.5")
    crit = [{"id": "no_v3", "forbid": re.compile("V3"), "description": "no V3"}]
    # analyst only appends (never removes the V3 the seed carries), so every answer keeps emitting V3
    lm = _fake_lm({"analyst": json.dumps(
        {"failure_summary": [], "patch": {"edits": [{"op": "append", "content": "GOOD"}]}})})
    _install(monkeypatch, lm, scorer=lambda s: 1.0 if "GOOD" in s else 0.0,
             answer_fn=lambda s: "uses V3 directive" if "V3" in s else "clean v4")
    best, _, best_score = S.run_skillopt({"body": "SEED with V3"}, TRAIN, acceptance=crit,
                                         log=lambda *a: None)
    # judge loves the append (1.0) but every answer still violates -> soft docked by the full penalty
    assert best_score == pytest.approx(0.5)


def test_acceptance_violation_steers_reflection_to_remove(monkeypatch):
    import re
    monkeypatch.setenv("SKILLOPT_EPOCHS", "1")
    monkeypatch.setenv("SKILLOPT_MINIBATCH", "10")
    crit = [{"id": "no_v3", "forbid": re.compile("V3"), "description": "no V3"}]
    lm = _fake_lm({"analyst": json.dumps(
        {"failure_summary": [], "patch": {"edits": [{"op": "append", "content": "X"}]}})})
    _install(monkeypatch, lm, scorer=lambda s: 0.0, answer_fn=lambda s: "emits V3")
    S.run_skillopt({"body": "SEED"}, TRAIN, acceptance=crit, log=lambda *a: None)
    assert any("REMOVED" in m for m in lm.seen["reflect_user_msgs"])   # deletion hint reached reflection


# ── pure helper functions ──────────────────────────────────────────────────────

def test_format_buffer_shows_failures_and_rejected_edits():
    buffer = [{"failures": [{"description": "misses @import"}], "rejected": []},
              {"failures": [], "rejected": [{"op": "append", "content": "BAD RULE", "target": ""}]}]
    txt = S._format_buffer(buffer)
    assert "recurring failure: misses @import" in txt
    assert "REJECTED edit" in txt and "BAD RULE" in txt


def test_longitudinal_categorizes_task_transitions():
    tasks = [{"task": "a"}, {"task": "b"}, {"task": "c"}, {"task": "d"}]
    before = {"a": True, "b": True, "c": False, "d": False}
    after = {"a": True, "b": False, "c": True, "d": False}
    txt = S._longitudinal(tasks, before, after)
    assert "stable successes (1)" in txt              # a: pass -> pass
    assert "regressions (1)" in txt                   # b: pass -> fail
    assert "improvements (1)" in txt                  # c: fail -> pass
    assert "persistent failures (1)" in txt           # d: fail -> fail


def test_inject_slow_update_appends_then_replaces_the_region():
    once = S._inject_slow_update("# Skill\nrules", "guidance one")
    assert S._SLOW_START in once and "guidance one" in once
    twice = S._inject_slow_update(once, "guidance two")
    assert "guidance two" in twice and "guidance one" not in twice   # replaced, not duplicated
    assert twice.count(S._SLOW_START) == 1
