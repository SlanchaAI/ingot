"""Best-of-N racing strategy: candidate parsing, halving schedule, winner selection, seed fallback.
All model calls are stubbed — hermetic."""
import json

import pytest

from optimize import bestofn


TRAIN = [{"task": f"t{i}", "rubric": f"r{i}"} for i in range(4)]


def test_parse_single_component_raw_text():
    assert bestofn._parse_candidate("# New body\nrules", {"body": "old"}) == {"body": "# New body\nrules"}


def test_parse_single_component_tolerates_fence():
    assert bestofn._parse_candidate("```markdown\n# New body\n```", {"body": "old"}) == {"body": "# New body"}


def test_parse_multi_component_json_with_prose():
    seed = {"body": "b", "description": "d"}
    reply = 'Here you go:\n{"body": "new b", "description": "new d"} hope that helps'
    assert bestofn._parse_candidate(reply, seed) == {"body": "new b", "description": "new d"}


def test_parse_rejects_garbage():
    assert bestofn._parse_candidate("", {"body": "old"}) is None
    assert bestofn._parse_candidate("not json at all", {"body": "b", "description": "d"}) is None


def _stub_rollouts(monkeypatch, scores_by_body: dict[str, float], calls: list):
    """Stub SkillAdapter._rollout: score keyed by candidate body (embedded in the system prompt)."""
    def rollout(self, system, ex):
        calls.append((system, ex["task"]))
        score = next((v for k, v in scores_by_body.items() if k in system), 0.0)
        return "answer", score, {"task": ex["task"], "output": "answer",
                                 "feedback": "fb", "dimensions": {}}
    monkeypatch.setattr(bestofn.SkillAdapter, "_rollout", rollout)


def test_race_picks_best_and_halves(monkeypatch):
    calls = []
    _stub_rollouts(monkeypatch, {"SEED": 0.2, "CAND-0": 0.4, "CAND-1": 0.9, "CAND-2": 0.6}, calls)

    def lm(prompt):   # keyed off the angle so concurrent authors stay deterministic
        i = next((i for i, a in enumerate(bestofn._ANGLES) if a in prompt), None)
        return "" if i is None else f"CAND-{i}"   # the diagnosis prompt carries no angle
    monkeypatch.setattr(bestofn, "make_reflection_lm", lambda: lm)

    best, seed_score, best_score = bestofn.run_bestofn({"body": "SEED"}, TRAIN,
                                                       candidates=3, log=lambda *a: None)
    assert best == {"body": "CAND-1"}
    assert seed_score == pytest.approx(0.2)
    assert best_score == pytest.approx(0.9)
    # rollout budget: 4 seed + racing 3+2+2+2 (min-2 floor) = 13, far under the 60 metric calls
    # the removed sequential body loop spent for the same objective
    assert len(calls) == 13


def test_keeps_seed_when_no_candidate_beats_it(monkeypatch):
    _stub_rollouts(monkeypatch, {"SEED": 0.9, "CAND": 0.1}, [])
    monkeypatch.setattr(bestofn, "make_reflection_lm", lambda: lambda prompt: "CAND")
    best, seed_score, best_score = bestofn.run_bestofn({"body": "SEED"}, TRAIN,
                                                       candidates=2, log=lambda *a: None)
    assert best == {"body": "SEED"}
    assert best_score <= seed_score


def test_keeps_seed_when_all_authors_fail(monkeypatch):
    _stub_rollouts(monkeypatch, {"SEED": 0.5}, [])
    monkeypatch.setattr(bestofn, "make_reflection_lm", lambda: lambda prompt: "")
    best, seed_score, best_score = bestofn.run_bestofn({"body": "SEED"}, TRAIN,
                                                       candidates=2, log=lambda *a: None)
    assert best == {"body": "SEED"} and seed_score == best_score


def test_author_prompt_carries_failures_and_distinct_angles(monkeypatch):
    _stub_rollouts(monkeypatch, {"SEED": 0.0, "CAND": 1.0}, [])
    prompts = []

    def lm(prompt):
        prompts.append(prompt)
        return "CAND"
    monkeypatch.setattr(bestofn, "make_reflection_lm", lambda: lambda p: lm(p))
    bestofn.run_bestofn({"body": "SEED"}, TRAIN, candidates=3, log=lambda *a: None)
    authors = [p for p in prompts if "Angle for THIS draft:" in p]   # diagnosis call excluded
    assert len(authors) == 3
    assert all("feedback: fb" in p for p in authors)          # failure evidence briefed in
    assert len({p.split("Angle for THIS draft:")[1].split("\n")[0] for p in authors}) == 3


def test_length_penalty_applies_to_candidates(monkeypatch):
    long_body = "CAND-LONG " + "x" * (bestofn.length_penalty.__globals__["BODY_TARGET_CHARS"] * 3)
    _stub_rollouts(monkeypatch, {"SEED": 0.5, "CAND-LONG": 0.55, "CAND-SHORT": 0.54}, [])

    def lm(prompt):
        return long_body if bestofn._ANGLES[0] in prompt else "CAND-SHORT"
    monkeypatch.setattr(bestofn, "make_reflection_lm", lambda: lm)
    best, _, _ = bestofn.run_bestofn({"body": "SEED"}, TRAIN, candidates=2, log=lambda *a: None)
    assert best == {"body": "CAND-SHORT"}   # 0.55 - max penalty loses to 0.54 unpenalized


def test_parse_multi_component_fenced_json():
    seed = {"body": "b", "description": "d"}
    reply = '```json\n{"body": "new b", "description": "new d", "extra": "ignored"}\n```'
    assert bestofn._parse_candidate(reply, seed) == {"body": "new b", "description": "new d"}


def test_parse_multi_component_rejects_missing_component():
    assert bestofn._parse_candidate('{"body": "only body"}', {"body": "b", "description": "d"}) is None


def test_halving_schedule_ten_candidates(monkeypatch):
    scores = {"SEED": 0.1, **{f"CAND-{i}": 0.2 + i * 0.05 for i in range(10)}}
    calls = []
    _stub_rollouts(monkeypatch, scores, calls)

    # angles cycle at 6, so key drafts off a locked counter instead of the angle text
    import itertools
    import threading
    counter = itertools.count()
    lock = threading.Lock()

    def lm_indexed(prompt):
        if "Angle for THIS draft:" not in prompt:   # diagnosis call must not consume an index
            return ""
        with lock:
            return f"CAND-{next(counter)}"
    monkeypatch.setattr(bestofn, "make_reflection_lm", lambda: lm_indexed)

    best, _, best_score = bestofn.run_bestofn({"body": "SEED"}, TRAIN,
                                              candidates=10, log=lambda *a: None)
    assert best == {"body": "CAND-9"} and best_score == pytest.approx(0.65)
    # 4 seed rollouts + race waves 10 -> 5 -> 3 -> 2 (ceil halving, floor 2)
    assert len(calls) == 4 + 10 + 5 + 3 + 2


def test_single_train_task_single_round(monkeypatch):
    calls = []
    _stub_rollouts(monkeypatch, {"SEED": 0.1, "CAND-0": 0.3, "CAND-1": 0.8}, calls)

    def lm(prompt):
        i = next((i for i, a in enumerate(bestofn._ANGLES) if a in prompt), None)
        return "" if i is None else f"CAND-{i}"
    monkeypatch.setattr(bestofn, "make_reflection_lm", lambda: lm)
    best, _, _ = bestofn.run_bestofn({"body": "SEED"}, TRAIN[:1], candidates=2, log=lambda *a: None)
    assert best == {"body": "CAND-1"}
    assert len(calls) == 1 + 2   # one seed rollout, one race round with both candidates


def test_empty_train_set_keeps_seed(monkeypatch):
    monkeypatch.setattr(bestofn, "make_reflection_lm",
                        lambda: (_ for _ in ()).throw(AssertionError("must not author")))
    assert bestofn.run_bestofn({"body": "SEED"}, [], candidates=3,
                               log=lambda *a: None) == ({"body": "SEED"}, 0.0, 0.0)


def test_one_failing_author_does_not_kill_the_wave(monkeypatch):
    _stub_rollouts(monkeypatch, {"SEED": 0.1, "CAND-OK": 0.9}, [])

    def lm(prompt):
        if bestofn._ANGLES[0] in prompt:
            raise RuntimeError("provider 500")
        return "CAND-OK"
    monkeypatch.setattr(bestofn, "make_reflection_lm", lambda: lm)
    best, _, best_score = bestofn.run_bestofn({"body": "SEED"}, TRAIN,
                                              candidates=2, log=lambda *a: None)
    assert best == {"body": "CAND-OK"} and best_score == pytest.approx(0.9)


def test_multi_component_end_to_end(monkeypatch):
    seed = {"body": "SEED-BODY", "description": "SEED-DESC"}
    _stub_rollouts(monkeypatch, {"SEED-BODY": 0.2, "NEW-BODY": 0.8}, [])
    reply = json.dumps({"body": "NEW-BODY", "description": "NEW-DESC"})
    monkeypatch.setattr(bestofn, "make_reflection_lm", lambda: lambda p: reply)
    best, _, _ = bestofn.run_bestofn(seed, TRAIN, candidates=2, log=lambda *a: None)
    assert best == {"body": "NEW-BODY", "description": "NEW-DESC"}


def test_champion_cache_key_changes_with_revision_and_tasks():
    from optimize.ab import _champion_cache_key
    holdout = [{"task": "a", "rubric": "r"}]
    k = _champion_cache_key("rev1", holdout)
    assert k == _champion_cache_key("rev1", json.loads(json.dumps(holdout)))
    assert k != _champion_cache_key("rev2", holdout)
    assert k != _champion_cache_key("rev1", holdout + [{"task": "b"}])


def test_champion_cache_key_changes_with_judge_model(monkeypatch):
    from optimize.ab import _champion_cache_key
    holdout = [{"task": "a"}]
    k1 = _champion_cache_key("rev", holdout)
    monkeypatch.setenv("JUDGE_MODEL", "some/other-judge")
    assert _champion_cache_key("rev", holdout) != k1


def test_author_prompt_protects_passing_tasks_from_deletion(monkeypatch):
    # seed passes t0/t2 (1.0) and fails t1/t3 — authors must be told what to preserve
    scores = {"SEED": 0.0, "CAND": 1.0}
    def rollout(self, system, ex):
        s = 1.0 if ("SEED" in system and ex["task"] in ("t0", "t2")) else scores.get(
            next((k for k in scores if k in system), ""), 0.0)
        return "a", s, {"task": ex["task"], "output": "a", "feedback": "fb", "dimensions": {}}
    monkeypatch.setattr(bestofn.SkillAdapter, "_rollout", rollout)
    prompts = []

    def lm(p):
        prompts.append(p)
        return "CAND"
    monkeypatch.setattr(bestofn, "make_reflection_lm", lambda: lm)
    bestofn.run_bestofn({"body": "SEED"}, TRAIN, candidates=2, log=lambda *a: None)
    authors = [p for p in prompts if "Angle for THIS draft:" in p]
    assert all("Deletions need evidence" in p for p in authors)
    assert all("- t0 (judge score 1.00)" in p and "- t2 (judge score 1.00)" in p for p in authors)
    assert all("- t1 (judge score" not in p for p in authors)   # failing tasks aren't "preserve" items


def _baseline(score, dims):
    return [("a", score, {"task": "t", "output": "a", "feedback": "fb", "dimensions": dims})]


def test_research_skips_without_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_KEY", raising=False)
    assert bestofn.research_brief(_baseline(0.0, {"correctness": "wrong API"}), log=lambda *a: None) == ""


def test_research_skips_non_knowledge_failures(monkeypatch):
    monkeypatch.setenv("TAVILY_KEY", "tvly-test")
    monkeypatch.setattr(bestofn, "_tavily_search",
                        lambda q, k: (_ for _ in ()).throw(AssertionError("must not search")))
    fmt_fail = {"correctness": "pass", "instruction_following": "added prose"}
    assert bestofn.research_brief(_baseline(0.4, fmt_fail), log=lambda *a: None) == ""


def test_research_briefs_and_caches(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_KEY", "tvly-test")
    monkeypatch.setattr(bestofn, "_RESEARCH_CACHE", tmp_path)
    calls = []

    def fake_search(q, k):
        calls.append(q)
        return "FIL is the answer"
    monkeypatch.setattr(bestofn, "_tavily_search", fake_search)
    base = _baseline(0.0, {"correctness": "named the wrong library"})
    brief1 = bestofn.research_brief(base, log=lambda *a: None)
    brief2 = bestofn.research_brief(base, log=lambda *a: None)   # cache hit — no new searches
    assert "FIL is the answer" in brief1 and brief1 == brief2
    assert len(calls) == 1


def test_research_reaches_author_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_KEY", "tvly-test")
    monkeypatch.setattr(bestofn, "_RESEARCH_CACHE", tmp_path)
    monkeypatch.setattr(bestofn, "_tavily_search", lambda q, k: "USE @theme IN V4")
    def rollout(self, system, ex):
        s = 0.0 if "SEED" in system else 1.0
        return "a", s, {"task": ex["task"], "output": "a", "feedback": "fb",
                        "dimensions": {"correctness": "v3 answer"}}
    monkeypatch.setattr(bestofn.SkillAdapter, "_rollout", rollout)
    prompts = []
    monkeypatch.setattr(bestofn, "make_reflection_lm", lambda: lambda p: prompts.append(p) or "CAND")
    bestofn.run_bestofn({"body": "SEED"}, TRAIN, candidates=2, log=lambda *a: None)
    authors = [p for p in prompts if "Angle for THIS draft:" in p]
    assert authors and all("USE @theme IN V4" in p for p in authors)
    assert all("authoritative over your priors" in p for p in authors)


# Diagnosis (SkillForge's Skill Diagnostician) + the minimal-edit author angle


def test_minimal_edit_angle_rides_in_default_wave():
    # the SkillForge Do-No-Harm angle must sit in the first OPTIMIZE_CANDIDATES(=5) slots
    assert "smallest additive edit" in bestofn._ANGLES[0]


def test_diagnosis_reaches_author_prompts(monkeypatch):
    _stub_rollouts(monkeypatch, {"SEED": 0.0, "CAND": 1.0}, [])
    prompts = []

    def lm(p):
        prompts.append(p)
        return "IMPLICATED: install section (incorrect)" if "diagnosing an agent skill" in p else "CAND"
    monkeypatch.setattr(bestofn, "make_reflection_lm", lambda: lm)
    bestofn.run_bestofn({"body": "SEED"}, TRAIN, candidates=2, log=lambda *a: None)
    diags = [p for p in prompts if "diagnosing an agent skill" in p]
    authors = [p for p in prompts if "Angle for THIS draft:" in p]
    assert len(diags) == 1                                     # one diagnosis call per run
    assert "insufficient" in diags[0] and "feedback: fb" in diags[0]   # defect taxonomy + evidence
    assert all("IMPLICATED: install section (incorrect)" in p for p in authors)
    assert all("keep what PRESERVE names" in p for p in authors)


def test_diagnosis_skipped_when_seed_passes_everything(monkeypatch):
    _stub_rollouts(monkeypatch, {"SEED": 1.0, "CAND": 1.0}, [])
    prompts = []
    monkeypatch.setattr(bestofn, "make_reflection_lm", lambda: lambda p: prompts.append(p) or "CAND")
    bestofn.run_bestofn({"body": "SEED"}, TRAIN, candidates=2, log=lambda *a: None)
    assert not any("diagnosing an agent skill" in p for p in prompts)


def test_diagnosis_failure_does_not_kill_run(monkeypatch):
    _stub_rollouts(monkeypatch, {"SEED": 0.1, "CAND": 0.9}, [])

    def lm(p):
        if "diagnosing an agent skill" in p:
            raise RuntimeError("provider 500")
        return "CAND"
    monkeypatch.setattr(bestofn, "make_reflection_lm", lambda: lm)
    best, _, best_score = bestofn.run_bestofn({"body": "SEED"}, TRAIN,
                                              candidates=2, log=lambda *a: None)
    assert best == {"body": "CAND"} and best_score == pytest.approx(0.9)
