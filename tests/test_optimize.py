"""Unit tests for optimizer pure-logic (no network/LLM): the canary's Thompson decision and the
multi-dimensional judge's failure parsing."""
import numpy as np

from optimize import ab as ab_mod
from optimize import judge as judge_mod
from optimize.ab import promotion_gate
from optimize.canary import p_challenger_better
from optimize.judge import DIMENSIONS, failed_dimensions


def test_ensemble_judge_averages_score_and_majority_votes_dimensions(monkeypatch):
    # two judges: one says 1.0 all-pass, one says 0.0 with a correctness failure -> mean 0.5, and
    # correctness fails only if a MAJORITY flag it (here 1 of 2 → still "pass", harder to game)
    fake = iter([
        {"score": 1.0, "feedback": "great", "dimensions": {d: "pass" for d in DIMENSIONS}},
        {"score": 0.0, "feedback": "wrong", "dimensions": {**{d: "pass" for d in DIMENSIONS}, "correctness": "bad API"}},
    ])
    monkeypatch.setattr(judge_mod, "MODELS", ["m1", "m2"])
    monkeypatch.setattr(judge_mod, "_judge_one", lambda model, prompt: next(fake))
    r = judge_mod.judge("t", "rubric", "ans")
    assert abs(r["score"] - 0.5) < 1e-9
    assert failed_dimensions(r["dimensions"]) == []   # 1/2 is not a majority → not flagged


def test_canary_prefers_the_arm_with_more_successes():
    # challenger 9/10 successes vs champion 1/10 → P(challenger better) ~ 1
    champ = {"a": 2.0, "b": 10.0}      # 1 success, 9 fail (+1 prior each)
    chall = {"a": 10.0, "b": 2.0}      # 9 success, 1 fail
    assert p_challenger_better(champ, chall) > 0.95


def test_canary_is_uncertain_with_no_evidence():
    flat = {"a": 1.0, "b": 1.0}        # uniform prior, no samples
    assert 0.35 < p_challenger_better(flat, dict(flat)) < 0.65


def test_canary_rejects_a_worse_challenger():
    champ = {"a": 10.0, "b": 2.0}
    chall = {"a": 2.0, "b": 10.0}
    assert p_challenger_better(champ, chall) < 0.05


def test_gate_passes_a_clean_generalizing_win():
    ok, reasons = promotion_gate("pdf", [0.2, 0.2, 0.2], [0.9, 0.9, 0.9], changed=[], challenger={})
    assert ok and reasons == []


def test_gate_blocks_a_thin_margin():
    ok, reasons = promotion_gate("pdf", [0.5, 0.5, 0.5], [0.55, 0.55, 0.55], changed=[], challenger={})
    assert not ok and any("margin" in r for r in reasons)


def test_gate_blocks_too_few_samples():
    ok, reasons = promotion_gate("pdf", [0.1], [0.9], changed=[], challenger={})
    assert not ok and any("held-out tasks" in r for r in reasons)


def test_gate_blocks_catastrophic_regression():
    # challenger wins the mean (+0.33) but breaks a task the champion passed
    ok, reasons = promotion_gate("pdf", [1.0, 0.0, 0.0], [0.0, 1.0, 1.0], changed=[], challenger={})
    assert not ok and any("regression" in r for r in reasons)


def test_gate_blocks_a_route_shadowing_description(monkeypatch):
    # a clean quality win, but the rewritten description near-duplicates another skill's -> routing hack
    monkeypatch.setattr(ab_mod, "_description_shadows", lambda skill, desc: ("docx", 0.97))
    ok, reasons = promotion_gate("pdf", [0.2, 0.2, 0.2], [0.9, 0.9, 0.9],
                                 changed=["description"], challenger={"description": "…"})
    assert not ok and any("shadow" in r for r in reasons)


def test_gate_allows_a_distinct_description_change(monkeypatch):
    monkeypatch.setattr(ab_mod, "_description_shadows", lambda skill, desc: ("docx", 0.40))
    ok, reasons = promotion_gate("pdf", [0.2, 0.2, 0.2], [0.9, 0.9, 0.9],
                                 changed=["description"], challenger={"description": "…"})
    assert ok and reasons == []


def test_load_tasks_flat_list_falls_back_to_no_split(tmp_path, monkeypatch):
    (tmp_path / "pdf.yaml").write_text("skill: pdf\ntasks:\n  - task: t1\n    rubric: r1\n  - task: t2\n    rubric: r2\n")
    monkeypatch.setattr(ab_mod, "TASKS_DIR", tmp_path)
    train, holdout = ab_mod.load_tasks("pdf")
    assert train == holdout and len(train) == 2   # a flat `tasks:` list -> train == holdout (no split)


def test_load_tasks_reads_explicit_train_holdout(tmp_path, monkeypatch):
    (tmp_path / "pdf.yaml").write_text(
        "skill: pdf\ntrain:\n  - task: a\n    rubric: r\nholdout:\n  - task: b\n    rubric: r\n  - task: c\n    rubric: r\n")
    monkeypatch.setattr(ab_mod, "TASKS_DIR", tmp_path)
    train, holdout = ab_mod.load_tasks("pdf")
    assert [t["task"] for t in train] == ["a"] and [t["task"] for t in holdout] == ["b", "c"]


def test_canary_probability_is_deterministic_under_seed():
    champ, chall = {"a": 3.0, "b": 5.0}, {"a": 6.0, "b": 2.0}
    np.random.seed(0); a = p_challenger_better(champ, chall)
    np.random.seed(0); b = p_challenger_better(champ, chall)
    assert a == b  # same seed -> identical estimate (no hidden nondeterminism)


def test_length_penalty_is_zero_under_target_and_grows_above():
    from optimize.gepa_loop import BODY_TARGET_CHARS, LENGTH_PENALTY, length_penalty
    assert length_penalty("x" * (BODY_TARGET_CHARS // 2)) == 0.0            # concise -> no penalty
    assert length_penalty("x" * BODY_TARGET_CHARS) == 0.0                   # exactly at target -> no penalty
    assert length_penalty("x" * (BODY_TARGET_CHARS * 2)) > 0.0             # bloated -> penalized
    assert length_penalty("x" * (BODY_TARGET_CHARS * 100)) == LENGTH_PENALTY  # capped, never unbounded


def test_failed_dimensions_flags_only_non_pass():
    dims = {"correctness": "pass", "completeness": "missing OCR", "instruction_following": "ok",
            "efficiency": "padded output"}
    assert set(failed_dimensions(dims)) == {"completeness", "efficiency"}
    assert failed_dimensions({d: "pass" for d in DIMENSIONS}) == []
