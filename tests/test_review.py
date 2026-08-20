"""Unit tests for the standalone per-skill review (LLM + judge mocked)."""
import json

import pytest

from ingot.optimize import review as R


def _result(task, checklist, spec):
    return {"task": task, "score": 0.0, "answer": "a", "feedback": "f",
            "checklist": checklist, "spec": spec}


SPEC = {
    "cites_source": {"id": "cites_source", "criterion": "Names its source.", "weight": 5,
                     "dimension": "correctness"},
    "is_terse": {"id": "is_terse", "criterion": "No padding.", "weight": 1,
                 "dimension": "efficiency"},
}


def test_findings_rank_by_cost_not_by_raw_score():
    """A heavy check scraping a partial outranks a trivial check failing outright. Sorting on the
    verdict value alone puts the weight-1 failure first and buries the thing worth fixing."""
    results = [_result("t", {"cites_source": {"value": 0.5, "note": "no source"},
                             "is_terse": {"value": 0.0, "note": "padded"}}, SPEC)]
    ranked = R.findings(results)
    assert [f["check"] for f in ranked] == ["cites_source", "is_terse"]
    assert ranked[0]["cost"] == pytest.approx(2.5) and ranked[1]["cost"] == pytest.approx(1.0)


def test_findings_omit_clean_passes():
    results = [_result("t", {"cites_source": {"value": 1.0, "note": ""},
                             "is_terse": {"value": 0.0, "note": "padded"}}, SPEC)]
    assert [f["check"] for f in R.findings(results)] == ["is_terse"]


def test_findings_default_weight_when_a_task_declared_no_spec():
    """A task with no checklist is graded on the default four, whose specs are not in `spec`.
    Those failures still have to appear, at weight 1, rather than vanish."""
    results = [_result("t", {"correctness": {"value": 0.0, "note": "wrong"}}, {})]
    found = R.findings(results)
    assert len(found) == 1 and found[0]["weight"] == 1 and found[0]["cost"] == pytest.approx(1.0)


def test_by_dimension_concentrates_losses():
    results = [_result("t", {"cites_source": {"value": 0.0, "note": "n"},
                             "is_terse": {"value": 0.5, "note": "n"}}, SPEC)]
    assert R.by_dimension(R.findings(results)) == {"correctness": 5.0, "efficiency": 0.5}


def test_by_dimension_drops_dimensions_with_no_losses():
    results = [_result("t", {"is_terse": {"value": 0.0, "note": "n"}}, SPEC)]
    assert R.by_dimension(R.findings(results)) == {"efficiency": 1.0}


def test_run_review_scores_grades_and_writes_a_report(tmp_path, monkeypatch):
    from ingot.mcp_server.registry import skill_revision
    tasks = [{"task": "t1", "rubric": "r", "checklist": [SPEC["cites_source"]]},
             {"task": "t2", "rubric": "r", "checklist": [SPEC["is_terse"]]}]
    monkeypatch.setattr(R, "resolve_skill_dir", lambda name: tmp_path)
    monkeypatch.setattr(R, "load_tasks", lambda skill, **_: (tasks[:1], tasks[1:], {}))
    monkeypatch.setattr(R, "optimizable_components", lambda d: {"description": "d", "body": "B"})
    monkeypatch.setattr(R, "assemble", lambda c: c["body"])
    monkeypatch.setattr(R, "_llm", lambda model: "llm")
    monkeypatch.setattr(R, "invoke_retry",
                        lambda llm, msgs: type("M", (), {"content": "ans", "usage_metadata": None})())
    monkeypatch.setattr(R, "judge", lambda task, rubric, answer, **kw: {
        "score": 0.5, "feedback": "f",
        "checklist": {c["id"]: {"value": 0.5, "note": "half"} for c in kw["checklist"]}})
    monkeypatch.setattr(R, "REVIEW_DIR", tmp_path / "reviews")
    expected_revision = skill_revision(tmp_path)

    out = R.run_review("sk", log=lambda *a: None)
    assert out["score"] == pytest.approx(0.5)
    assert out["tasks"] == 2 and out["failed_checks"] == 2
    written = json.loads((tmp_path / "reviews" / "sk.json").read_text())
    assert written["skill"] == "sk"
    assert written["revision"] == expected_revision
    assert [f["check"] for f in written["findings"]] == ["cites_source", "is_terse"]  # by cost


def test_run_review_grades_train_and_holdout_together(tmp_path, monkeypatch):
    """A review is not measuring generalization, so holding half the tasks back would only make it
    a noisier read on the same skill."""
    seen = []
    monkeypatch.setattr(R, "resolve_skill_dir", lambda name: tmp_path)
    monkeypatch.setattr(R, "load_tasks",
                        lambda skill, **_: ([{"task": "train", "rubric": "r"}],
                                            [{"task": "holdout", "rubric": "r"}], {}))
    monkeypatch.setattr(R, "optimizable_components", lambda d: {"description": "d", "body": "B"})
    monkeypatch.setattr(R, "assemble", lambda c: c["body"])
    monkeypatch.setattr(R, "_llm", lambda model: "llm")
    monkeypatch.setattr(R, "invoke_retry", lambda llm, msgs: (
        seen.append(msgs[1][1]) or type("M", (), {"content": "a", "usage_metadata": None})()))
    monkeypatch.setattr(R, "judge", lambda *a, **k: {"score": 1.0, "feedback": "",
                                                     "checklist": {}})
    monkeypatch.setattr(R, "REVIEW_DIR", tmp_path / "reviews")
    R.run_review("sk", log=lambda *a: None)
    assert sorted(seen) == ["holdout", "train"]


def test_run_review_hashes_and_grades_one_immutable_snapshot(tmp_path, monkeypatch):
    from ingot.mcp_server.registry import skill_revision, write_skill_md
    skill = tmp_path / "skills" / "sk"
    skill.mkdir(parents=True)
    write_skill_md(skill / "SKILL.md", {"name": "sk", "description": "old description"},
                   "old body")
    expected_revision = skill_revision(skill)
    seen_systems = []

    monkeypatch.setattr(R, "resolve_skill_dir", lambda name: skill)

    def mutate_then_load(name, **_):
        write_skill_md(skill / "SKILL.md", {"name": "sk", "description": "new description"},
                       "new body")
        return ([{"task": "t", "rubric": "r"}], [], {})

    monkeypatch.setattr(R, "load_tasks", mutate_then_load)
    monkeypatch.setattr(R, "_llm", lambda model: "llm")
    monkeypatch.setattr(R, "invoke_retry", lambda llm, msgs: (
        seen_systems.append(msgs[0][1]) or
        type("M", (), {"content": "a", "usage_metadata": None})()))
    monkeypatch.setattr(R, "judge", lambda *a, **k: {
        "score": 1.0, "feedback": "", "checklist": {}})
    monkeypatch.setattr(R, "REVIEW_DIR", tmp_path / "reviews")

    result = R.run_review("sk", log=lambda *a: None)

    assert result["revision"] == expected_revision
    assert seen_systems and "old body" in seen_systems[0] and "new body" not in seen_systems[0]


def test_run_review_drafts_missing_evals_from_the_reviewed_snapshot(tmp_path, monkeypatch):
    from pathlib import Path
    from ingot.mcp_server import registry
    from ingot.mcp_server.registry import write_skill_md
    from ingot.optimize import ab, draft
    root = tmp_path / "skills"
    skill = root / "sk"
    skill.mkdir(parents=True)
    write_skill_md(skill / "SKILL.md", {"name": "sk", "description": "old description"},
                   "old body")
    tasks = tmp_path / "tasks"
    captured = {}
    read_components = registry.read_components

    def mutate_before_live_read(path):
        if Path(path).resolve() == skill.resolve():
            write_skill_md(skill / "SKILL.md",
                           {"name": "sk", "description": "new description"}, "new body")
        return read_components(path)

    def draft_eval(name, description, body, out_dir, log=print):
        captured.update(description=description, body=body)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{name}.yaml").write_text(
            "train:\n- task: train\n  rubric: r\nholdout:\n- task: holdout\n  rubric: r\n")

    monkeypatch.setenv("INGOT_LIBRARY", str(root))
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(root))
    monkeypatch.setattr(ab, "TASKS_DIR", tasks)
    monkeypatch.setattr(registry, "read_components", mutate_before_live_read)
    monkeypatch.setattr(draft, "draft_and_save", draft_eval)
    monkeypatch.setattr(R, "resolve_skill_dir", lambda name: skill)
    monkeypatch.setattr(R, "_llm", lambda model: "llm")
    monkeypatch.setattr(R, "invoke_retry", lambda *a, **k: type(
        "M", (), {"content": "a", "usage_metadata": None})())
    monkeypatch.setattr(R, "judge", lambda *a, **k: {
        "score": 1.0, "feedback": "", "checklist": {}})
    monkeypatch.setattr(R, "REVIEW_DIR", tmp_path / "reviews")

    R.run_review("sk", log=lambda *a: None)

    assert captured == {"description": "old description", "body": "old body"}


def test_run_review_refuses_a_skill_with_no_tasks(tmp_path, monkeypatch):
    from ingot.mcp_server.registry import write_skill_md
    write_skill_md(tmp_path / "SKILL.md", {"name": "sk", "description": "d"}, "body")
    monkeypatch.setattr(R, "resolve_skill_dir", lambda name: tmp_path)
    monkeypatch.setattr(R, "load_tasks", lambda skill, **_: ([], [], {}))
    with pytest.raises(SystemExit, match="no eval tasks"):
        R.run_review("sk", log=lambda *a: None)
