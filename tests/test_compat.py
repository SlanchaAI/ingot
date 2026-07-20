"""Unit tests for cross-model compatibility (no network/LLM): rollouts+judge are stubbed so the
model sweep, the skill-vs-baseline lift, and the written matrix are exercised deterministically."""
import json

import pytest

from optimize import compat


def test_compat_models_parses_env_else_defaults(monkeypatch):
    monkeypatch.setenv("COMPAT_MODELS", " a , b ,c ")
    assert compat.compat_models() == ["a", "b", "c"]
    monkeypatch.delenv("COMPAT_MODELS", raising=False)
    monkeypatch.setattr(compat, "agent_model", lambda: "solo/model")
    assert compat.compat_models() == ["solo/model"]


def test_run_compat_sweeps_models_and_computes_lift(tmp_path, monkeypatch):
    (tmp_path / "tailwind").mkdir()
    (tmp_path / "tailwind" / "SKILL.md").write_text("x")
    monkeypatch.setattr(compat, "SKILLS_DIR", tmp_path)
    monkeypatch.setattr(compat, "COMPAT_DIR", tmp_path / "out")
    monkeypatch.setenv("COMPAT_MODELS", "m1,m2")
    monkeypatch.setattr(compat, "load_tasks",
                        lambda skill: ([], [{"task": "t", "rubric": "r"}, {"task": "u", "rubric": "r"}], {}))
    monkeypatch.setattr(compat, "optimizable_components", lambda d: {"description": "d", "body": "THEBODY"})
    monkeypatch.setattr(compat, "_llm", lambda model: model)   # pass the model name through as the "llm"
    # the skill arm serves THEBODY (score 0.9); the no-skill baseline does not (0.2)
    monkeypatch.setattr(compat, "_score", lambda llm, system, task: 0.9 if "THEBODY" in system else 0.2)

    out = compat.run_compat("tailwind", log=lambda *a: None)
    assert set(out["models"]) == {"m1", "m2"}
    m1 = out["models"]["m1"]
    assert m1["skill_mean"] == pytest.approx(0.9)
    assert m1["baseline_mean"] == pytest.approx(0.2)
    assert m1["lift"] == pytest.approx(0.7)
    assert out["tasks"] == 2
    written = json.loads((tmp_path / "out" / "tailwind.json").read_text())
    assert written["skill"] == "tailwind" and set(written["models"]) == {"m1", "m2"}


def test_run_compat_rejects_unknown_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(compat, "SKILLS_DIR", tmp_path)
    with pytest.raises(SystemExit, match="No skill named"):
        compat.run_compat("nope", log=lambda *a: None)
