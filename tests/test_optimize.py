"""Unit tests for optimizer pure-logic (no network/LLM): the canary's Thompson decision and the
multi-dimensional judge's failure parsing."""
import inspect

import numpy as np

from optimize import ab as ab_mod
from optimize import judge as judge_mod
from optimize.ab import body_retention, promotion_gate, retention_warnings
from optimize.canary import p_challenger_better
from optimize.judge import DIMENSIONS, failed_dimensions


def test_optimizer_has_no_activation_control():
    from optimize import canary as canary_mod
    from optimize import promote as promotion

    assert "promote_now" not in inspect.signature(ab_mod.run_ab).parameters
    assert "auto_promote" not in inspect.signature(canary_mod.run_canary).parameters
    assert not hasattr(promotion, "promote")


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
    routing = {"champion": {"top1": 1.0, "recall_at_3": 1.0, "no_route_precision": 1.0},
               "challenger": {"top1": 1.0, "recall_at_3": 1.0, "no_route_precision": 1.0},
               "parity": {"rate": 1.0, "total": 2}}
    ok, reasons = promotion_gate("pdf", [0.2, 0.2, 0.2], [0.9, 0.9, 0.9],
                                 changed=["description"], challenger={"description": "…"},
                                 routing_metrics=routing)
    assert ok and reasons == []


def test_gate_blocks_description_that_regresses_routing():
    ok, reasons = promotion_gate("pdf", [0.2, 0.2, 0.2], [0.9, 0.9, 0.9],
                                 changed=["description"], challenger={"description": "…"},
                                 routing_failures=["merge two PDFs"])
    assert not ok and any("routing regression" in reason for reason in reasons)


def test_gate_blocks_training_holdout_leakage():
    ok, reasons = promotion_gate("pdf", [0.2, 0.2, 0.2], [0.9, 0.9, 0.9],
                                 changed=[], challenger={}, leakage=True)
    assert not ok and any("holdout" in reason and "training" in reason for reason in reasons)


def test_gate_blocks_missing_or_weak_routing_suite_for_description_change():
    base = dict(skill="pdf", champ_scores=[0.2] * 3, chall_scores=[0.9] * 3,
                changed=["description"], challenger={"description": "new"})
    ok, reasons = promotion_gate(**base, routing_metrics=None)
    assert not ok and any("routing suite" in reason for reason in reasons)
    weak = {"challenger": {"recall_at_3": 0.8, "no_route_precision": 0.9, "top1": 0.7},
            "champion": {"recall_at_3": 1.0, "no_route_precision": 1.0, "top1": 1.0},
            "parity": {"rate": 0.5, "total": 2}}
    ok, reasons = promotion_gate(**base, routing_metrics=weak)
    assert not ok
    assert any("recall@3" in reason for reason in reasons)
    assert any("no-route" in reason for reason in reasons)
    assert any("parity" in reason for reason in reasons)


def test_load_tasks_flat_list_falls_back_to_no_split(tmp_path, monkeypatch):
    (tmp_path / "pdf.yaml").write_text("skill: pdf\ntasks:\n  - task: t1\n    rubric: r1\n  - task: t2\n    rubric: r2\n")
    monkeypatch.setattr(ab_mod, "TASKS_DIR", tmp_path)
    train, holdout, split = ab_mod.load_tasks("pdf")
    assert train == holdout and len(train) == 2
    assert split == {"kind": "none", "leakage": True}


def test_load_tasks_reads_explicit_train_holdout(tmp_path, monkeypatch):
    (tmp_path / "pdf.yaml").write_text(
        "skill: pdf\ntrain:\n  - task: a\n    rubric: r\nholdout:\n  - task: b\n    rubric: r\n  - task: c\n    rubric: r\n")
    monkeypatch.setattr(ab_mod, "TASKS_DIR", tmp_path)
    train, holdout, split = ab_mod.load_tasks("pdf")
    assert [t["task"] for t in train] == ["a"] and [t["task"] for t in holdout] == ["b", "c"]
    assert split == {"kind": "holdout", "leakage": False}


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


def test_body_retention_full_partial_empty():
    assert body_retention("a\nb\nc", "a\nb\nc\nnew line") == 1.0
    assert body_retention("a\nb\nc\nd", "a\nb\nrewritten") == 0.5
    assert body_retention("a\nb", "totally\nnew") == 0.0
    assert body_retention("", "anything") == 1.0          # nothing to lose
    assert body_retention("a\n  a  \n\n", "a") == 1.0     # stripped + blank-line tolerant


def test_retention_warns_on_big_deletion_with_thin_holdout():
    champion = {"body": "\n".join(f"line {i}" for i in range(10))}
    challenger = {"body": "a fresh contract"}
    warns = retention_warnings(champion, challenger, changed=["body"], samples=4)
    assert len(warns) == 1
    assert "drops 100%" in warns[0] and "4 held-out task(s)" in warns[0]


def test_retention_silent_when_content_kept_or_body_unchanged():
    champion = {"body": "keep me\nand me"}
    kept = {"body": "keep me\nand me\nplus a new rule"}
    assert retention_warnings(champion, kept, changed=["body"], samples=4) == []
    assert retention_warnings(champion, {"body": "all new"}, changed=["description"], samples=4) == []


def test_retention_threshold_is_inclusive(monkeypatch):
    champion = {"body": "a\nb"}
    half = {"body": "a\nnew"}                      # retention exactly 0.5
    assert retention_warnings(champion, half, changed=["body"], samples=4) == []   # >= default 0.5
    monkeypatch.setattr(ab_mod, "RETENTION_WARN", 0.6)
    warns = retention_warnings(champion, half, changed=["body"], samples=4)
    assert len(warns) == 1 and "drops 50%" in warns[0]


def test_gate_ignores_parity_with_no_cases():
    routing = {"champion": {"top1": 1.0, "recall_at_3": 1.0, "no_route_precision": 1.0},
               "challenger": {"top1": 1.0, "recall_at_3": 1.0, "no_route_precision": 1.0},
               "parity": {"rate": 0.0, "total": 0}}   # no parity cases -> rate is meaningless
    ok, reasons = promotion_gate("pdf", [0.2] * 3, [0.9] * 3, changed=["description"],
                                 challenger={"description": "distinct"}, routing_metrics=routing)
    assert ok and reasons == []


def test_zdr_provider_pinned_and_in_sync():
    from optimize.judge import ZDR_PROVIDER
    assert ZDR_PROVIDER == {"provider": {"zdr": True, "data_collection": "deny"}}
    from agent.run import ZDR_PROVIDER as agent_zdr
    assert agent_zdr == ZDR_PROVIDER  # duplicated literal (import-weight reasons) must not drift


def test_judge_llms_carry_zdr_extra_body(monkeypatch):
    captured = {}

    class FakeLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)
    monkeypatch.setattr(judge_mod, "ChatOpenAI", FakeLLM)
    judge_mod._llms.clear()
    judge_mod._get_llm("some/model")
    judge_mod._llms.clear()
    assert captured["extra_body"] == {"provider": {"zdr": True, "data_collection": "deny"}}


def test_optimize_split_default_is_body_only():
    champion = {"description": "d", "body": "b"}
    seed, frozen = ab_mod.optimize_split(champion)
    assert seed == {"body": "b"} and frozen == {"description": "d"}


def test_optimize_split_env_can_widen(monkeypatch):
    monkeypatch.setattr(ab_mod, "OPTIMIZE_COMPONENTS", ["description", "body"])
    seed, frozen = ab_mod.optimize_split({"description": "d", "body": "b"})
    assert seed == {"description": "d", "body": "b"} and frozen == {}


def test_optimize_split_rejects_unknown_component(monkeypatch):
    import pytest
    monkeypatch.setattr(ab_mod, "OPTIMIZE_COMPONENTS", ["bodyy"])
    with pytest.raises(SystemExit, match="bodyy"):
        ab_mod.optimize_split({"description": "d", "body": "b"})


def test_skill_adapter_renders_frozen_components():
    from optimize.gepa_loop import assemble
    frozen, candidate = {"description": "when to use me"}, {"body": "the rules"}
    text = assemble({**frozen, **candidate})
    assert "when to use me" in text and "the rules" in text


def test_optimize_split_accepts_file_components(monkeypatch):
    monkeypatch.setattr(ab_mod, "OPTIMIZE_COMPONENTS", ["body", "file:reference.md"])
    champion = {"description": "d", "body": "b", "file:reference.md": "ref"}
    seed, frozen = ab_mod.optimize_split(champion)
    assert seed == {"body": "b", "file:reference.md": "ref"}
    assert frozen == {"description": "d"}


def test_eval_serve_template_injects_body_and_contract():
    text = ab_mod.EVAL_SERVE_TEMPLATE.format(body="THE SKILL BODY")
    assert "THE SKILL BODY" in text
    assert "final answer must contain the complete deliverable" in text
    assert "# Loaded skill" in text


def test_rollouts_serve_the_exact_serving_contract(monkeypatch):
    from optimize import SERVE_TEMPLATE
    from optimize import gepa_loop as G
    captured = {}

    class FakeLLM:
        def invoke(self, messages):
            captured["system"] = messages[0][1]
            class Msg:
                content = "an answer"
                usage_metadata = None
            return Msg()
    adapter = G.SkillAdapter(frozen={"description": "trigger words"})
    adapter._llm = FakeLLM()
    monkeypatch.setattr(G, "judge",
                        lambda t, r, a, reference="", check=None, deliverable=None:
                        {"score": 1.0, "feedback": "f", "dimensions": {}})
    batch = adapter.evaluate([{"task": "t", "rubric": "r"}], {"body": "the rules"})
    assert batch.scores == [1.0]
    # inner loop and outer A/B must serve the identical contract text
    assert captured["system"] == SERVE_TEMPLATE.format(
        body=G.assemble({"description": "trigger words", "body": "the rules"}))
    assert "complete deliverable" in captured["system"]


def test_agent_rollout_mode_routes_through_the_scaffold(monkeypatch):
    from optimize import gepa_loop as G
    monkeypatch.setattr(G, "GEPA_ROLLOUTS", "agent")
    monkeypatch.setattr(G, "judge",
                        lambda t, r, a, reference="", check=None, deliverable=None:
                        {"score": 0.5, "feedback": "f", "dimensions": {}})
    seen = {}

    def fake_agent_rollout(self, system, task):
        seen["task"] = task
        seen["system_has_contract"] = "complete deliverable" in system
        return "scaffold answer"
    monkeypatch.setattr(G.SkillAdapter, "_agent_rollout", fake_agent_rollout)
    adapter = G.SkillAdapter()
    adapter._llm = None  # direct-mode client must not be touched in agent mode
    batch = adapter.evaluate([{"task": "t1", "rubric": "r"}], {"body": "b"})
    assert batch.outputs == ["scaffold answer"] and seen["task"] == "t1"
    assert seen["system_has_contract"]
