"""Unit tests for the SkillOpt body-pass loop (no network/LLM): rollouts and the reflection LM are
stubbed so the orchestration — accept on improvement, keep the seed otherwise, and buffer rejected
edits back into the next reflection — is exercised deterministically."""
import json

from optimize import rollout as R
from optimize import skillopt_loop as S


def _fake_lm(reply_for):
    """Route each call to a canned reply by which vendored prompt is in the system message, and
    record every analyst (reflect) user-message for buffer assertions."""
    seen = {"reflect_user_msgs": []}

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
        return reply_for.get("slow", json.dumps({"guidance": "g"}))  # slow_update.md

    lm.seen = seen
    return lm


def _install(monkeypatch, lm, scorer):
    """Wire the loop to a stub reflection LM and a body-content-based rollout scorer."""
    monkeypatch.setattr(S, "make_reflection_lm", lambda: lm)

    def _rollout(self, system, ex):
        return "ans", scorer(system), {"task": ex["task"], "feedback": "fb",
                                       "output": "ans", "dimensions": {}}
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
