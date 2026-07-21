"""Unit tests for candidate-generation pure logic (no network/LLM): the promotion gate, the CLI
contract, and the multi-dimensional judge's failure parsing."""
import inspect

import pytest

from optimize import ab as ab_mod
from optimize import judge as judge_mod
from optimize.ab import body_retention, promotion_gate, retention_warnings
from optimize.judge import DIMENSIONS, failed_dimensions


def test_optimizer_has_no_activation_control():
    # the canary module is deleted outright on this branch, the strongest form of "no
    # activation control"; the remaining assertions cover the surviving surfaces
    from optimize import promote as promotion

    assert "promote_now" not in inspect.signature(ab_mod.run_ab).parameters
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


def test_gate_blocks_acceptance_violation():
    # a clean mean win, but a holdout answer violated a hard invariant -> blocked regardless
    ok, reasons = promotion_gate("pdf", [0.2, 0.2, 0.2], [0.9, 0.9, 0.9], changed=[], challenger={},
                                 acceptance_violations=["acceptance 'no_init': 1/3 holdout answer(s) matched"])
    assert not ok and any("acceptance" in reason for reason in reasons)


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


def test_greedy_pick_spreads_across_failure_modes():
    import numpy as np
    from optimize.mine import _greedy_pick
    # two orthogonal "failure modes", two near-identical tasks in each; hardest overall is
    # index 0, but the second pick must come from the OTHER mode even though index 1 is harder
    vecs = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 1.0]], dtype=np.float32)
    difficulty = [0.9, 0.8, 0.5, 0.4]
    assert _greedy_pick(difficulty, vecs, k=2) == [0, 2]


def test_greedy_pick_skips_aced_and_excluded_tasks():
    import numpy as np
    from optimize.mine import _greedy_pick
    vecs = np.eye(3, dtype=np.float32)
    # one real candidate, one aced task (difficulty 0), one excluded (train near-duplicate)
    assert _greedy_pick([0.7, 0.0, -1.0], vecs, k=3) == [0]


def test_save_pending_archives_a_displaced_cross_pass_challenger(tmp_path, monkeypatch):
    from optimize import promote as promote_mod
    monkeypatch.setattr(promote_mod, "PENDING_DIR", tmp_path)
    promote_mod.save_pending("pdf", {"changed_components": ["body"], "created": 111})
    promote_mod.save_pending("pdf", {"changed_components": ["body"], "created": 222})   # same pass: overwrite
    assert len(list(tmp_path.glob("pdf*"))) == 1
    promote_mod.save_pending("pdf", {"changed_components": ["description"], "created": 333})
    import json
    archived = tmp_path / "pdf.displaced-222.json"
    assert json.loads(archived.read_text())["changed_components"] == ["body"]           # preserved
    assert json.loads((tmp_path / "pdf.json").read_text())["changed_components"] == ["description"]


def test_length_penalty_is_zero_under_target_and_grows_above():
    from optimize.rollout import BODY_TARGET_CHARS, LENGTH_PENALTY, length_penalty
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
    monkeypatch.setattr(ab_mod, "OPTIMIZE_COMPONENTS", ["bodyy"])
    with pytest.raises(SystemExit, match="bodyy"):
        ab_mod.optimize_split({"description": "d", "body": "b"})


def test_skill_adapter_renders_frozen_components():
    from optimize.rollout import assemble
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
    from optimize import rollout as R
    captured = {}

    class FakeLLM:
        def invoke(self, messages):
            captured["system"] = messages[0][1]
            class Msg:
                content = "an answer"
                usage_metadata = None
            return Msg()
    adapter = R.SkillAdapter(frozen={"description": "trigger words"})
    adapter._llm = FakeLLM()
    monkeypatch.setattr(R, "judge",
                        lambda t, r, a, reference="", check=None, deliverable=None:
                        {"score": 1.0, "feedback": "f", "dimensions": {}})
    candidate = {"body": "the rules"}
    answer, score, _ = adapter._rollout(adapter.serve(candidate), {"task": "t", "rubric": "r"})
    assert (answer, score) == ("an answer", 1.0)
    # candidate search and the held-out A/B must serve the identical contract text
    assert captured["system"] == SERVE_TEMPLATE.format(
        body=R.assemble({"description": "trigger words", "body": "the rules"}))
    assert "complete deliverable" in captured["system"]


def test_agent_rollout_mode_routes_through_the_scaffold(monkeypatch):
    from optimize import rollout as R
    monkeypatch.setattr(R, "GEPA_ROLLOUTS", "agent")
    monkeypatch.setattr(R, "judge",
                        lambda t, r, a, reference="", check=None, deliverable=None:
                        {"score": 0.5, "feedback": "f", "dimensions": {}})
    seen = {}

    def fake_agent_rollout(self, system, task):
        seen["task"] = task
        seen["system_has_contract"] = "complete deliverable" in system
        return "scaffold answer"
    monkeypatch.setattr(R.SkillAdapter, "_agent_rollout", fake_agent_rollout)
    adapter = R.SkillAdapter()
    adapter._llm = None  # direct-mode client must not be touched in agent mode
    answer, _, _ = adapter._rollout(adapter.serve({"body": "b"}), {"task": "t1", "rubric": "r"})
    assert answer == "scaffold answer" and seen["task"] == "t1"
    assert seen["system_has_contract"]


def test_removed_gepa_body_loop_leaves_one_candidate_search():
    """The sequential GEPA body loop is gone: best-of-N is the only candidate search, and an
    inherited OPTIMIZE_STRATEGY must say so rather than silently mean nothing."""
    from optimize import rollout as R
    assert not hasattr(R, "run_gepa")
    with pytest.raises(ImportError):
        import optimize.gepa_loop  # noqa: F401
    assert "strategy" not in inspect.signature(ab_mod.run_ab).parameters


@pytest.mark.parametrize("flag", ["--gepa", "--skip-gepa", "--strategy", "--candidates"])
def test_cli_rejects_flags_for_passes_that_do_not_exist(flag):
    """A flag naming a removed or never-shipped pass must fail the parse, not be quietly ignored.
    Asserting on the parser rather than the source text is what proves the refusal."""
    with pytest.raises(SystemExit):
        ab_mod.parse_args(["pdf", flag])


def test_cli_accepts_the_passes_that_do_exist():
    body = ab_mod.parse_args(["pdf"])
    assert body.description is False and body.skill == "pdf"
    assert ab_mod.parse_args(["pdf", "--description"]).description is True
    assert ab_mod.parse_args(["pdf", "--scripts"]).scripts is True
    assert ab_mod.parse_args(["pdf", "--skip-search"]).skip_search is True


@pytest.mark.parametrize("pass_flags", [[], ["--scripts"]])
def test_cli_refuses_a_budget_only_the_routing_pass_honors(pass_flags):
    """--budget only bounds the routing pass's metric calls. Accepting it elsewhere would
    promise a limit nothing enforces."""
    with pytest.raises(SystemExit):
        ab_mod.parse_args(["pdf", *pass_flags, "--budget", "10"])
    assert ab_mod.parse_args(["pdf", "--description", "--budget", "10"]).budget == 10
    assert ab_mod.parse_args(["pdf", "--description"]).budget == ab_mod.DEFAULT_ROUTING_BUDGET


@pytest.mark.parametrize("flags", [["--body", "--description"], ["--body", "--scripts"],
                                   ["--description", "--scripts"]])
def test_cli_refuses_two_passes_at_once(flags):
    with pytest.raises(SystemExit):
        ab_mod.parse_args(["pdf", *flags])


# --- scripts pass -----------------------------------------------------------------------------

def _script_skill(tmp_path, monkeypatch, scripts=("scripts/helper.py",), holdout_check=True):
    import yaml
    skills = tmp_path / "skills"
    d = skills / "excel"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: excel\ndescription: Use for excel.\n---\nbody\n")
    for rel in scripts:
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("print('hi')\n")
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    holdout = [{"task": "t", "rubric": "r"}]
    if holdout_check:
        holdout[0]["check"] = {"fixture": "x = 1", "assert": "assert x == 1"}
    (tasks / "excel.yaml").write_text(yaml.safe_dump(
        {"train": [{"task": "t2", "rubric": "r"}], "holdout": holdout}))
    monkeypatch.setattr(ab_mod, "SKILLS_DIR", skills)
    monkeypatch.setattr(ab_mod, "TASKS_DIR", tasks)
    return d


def test_script_pass_components_names_the_bundled_scripts(tmp_path, monkeypatch):
    _script_skill(tmp_path, monkeypatch, scripts=("scripts/b.py", "scripts/a.sh"))
    assert ab_mod.script_pass_components("excel") == ["file:scripts/a.sh", "file:scripts/b.py"]


def test_script_pass_refuses_skill_without_scripts(tmp_path, monkeypatch):
    _script_skill(tmp_path, monkeypatch, scripts=())
    with pytest.raises(SystemExit, match="bundles no scripts"):
        ab_mod.script_pass_components("excel")


def test_script_pass_refuses_without_exec_checks(tmp_path, monkeypatch):
    """The LLM judge can't tell a broken script from a working one, so the pass must refuse to
    produce judge-only evidence for script changes."""
    _script_skill(tmp_path, monkeypatch, holdout_check=False)
    with pytest.raises(SystemExit, match="execution-grounded"):
        ab_mod.script_pass_components("excel")


def test_served_injects_bare_body_without_file_components():
    assert ab_mod._served({"description": "d", "body": "the body"}) == "the body"


def test_served_assembles_files_when_present():
    served = ab_mod._served({"description": "d", "body": "the body",
                             "file:scripts/h.py": "print(1)"})
    assert "the body" in served and "# scripts/h.py" in served and "print(1)" in served


def test_greedy_search_trains_each_component_with_the_others_frozen(tmp_path, monkeypatch):
    monkeypatch.setattr(ab_mod, "TASKS_DIR", tmp_path)  # no acceptance file -> no criteria
    calls = []

    def fake_skillopt(seed, tasks, frozen=None, acceptance=None, log=print):
        (key, text), = seed.items()
        calls.append({"key": key, "frozen": dict(frozen)})
        return {key: text + "!"}, 0.5, 0.9

    monkeypatch.setattr("optimize.skillopt_loop.run_skillopt", fake_skillopt)
    champion = {"description": "d", "body": "b",
                "file:scripts/a.py": "A", "file:scripts/b.py": "B"}
    challenger, seed_score, best_score = ab_mod._greedy_search(
        "excel", champion, ["file:scripts/a.py", "file:scripts/b.py"], [{"task": "t", "rubric": "r"}],
        log=lambda *_: None)

    assert [c["key"] for c in calls] == ["file:scripts/a.py", "file:scripts/b.py"]
    # each run renders the frozen text components plus the OTHER script at its latest text
    assert calls[0]["frozen"] == {"description": "d", "body": "b", "file:scripts/b.py": "B"}
    assert calls[1]["frozen"] == {"description": "d", "body": "b", "file:scripts/a.py": "A!"}
    assert challenger == {"description": "d", "body": "b",
                          "file:scripts/a.py": "A!", "file:scripts/b.py": "B!"}
    assert (seed_score, best_score) == (0.5, 0.9)


def test_greedy_search_body_pass_matches_the_old_single_run_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(ab_mod, "TASKS_DIR", tmp_path)
    calls = []

    def fake_skillopt(seed, tasks, frozen=None, acceptance=None, log=print):
        calls.append({"seed": dict(seed), "frozen": dict(frozen)})
        return {"body": "better"}, 0.3, 0.8

    monkeypatch.setattr("optimize.skillopt_loop.run_skillopt", fake_skillopt)
    champion = {"description": "d", "body": "b"}
    challenger, s0, s1 = ab_mod._greedy_search("excel", champion, ["body"],
                                               [{"task": "t", "rubric": "r"}], log=lambda *_: None)
    # one run, body mutable, description (and only text components) frozen -> unchanged behavior
    assert calls == [{"seed": {"body": "b"}, "frozen": {"description": "d"}}]
    assert challenger == {"description": "d", "body": "better"} and (s0, s1) == (0.3, 0.8)


def test_champion_cache_key_depends_on_served_components():
    holdout = [{"task": "t", "rubric": "r"}]
    body_only = ab_mod._champion_cache_key("rev", holdout, ["description", "body"])
    with_scripts = ab_mod._champion_cache_key("rev", holdout,
                                              ["description", "body", "file:scripts/a.py"])
    assert body_only != with_scripts
