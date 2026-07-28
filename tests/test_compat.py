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
    monkeypatch.setattr(compat, "resolve_skill_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(compat, "COMPAT_DIR", tmp_path / "out")
    monkeypatch.setenv("COMPAT_MODELS", "m1,m2")
    monkeypatch.setattr(compat, "load_tasks",
                        lambda skill: ([], [{"task": "t", "rubric": "r"}, {"task": "u", "rubric": "r"}], {}))
    monkeypatch.setattr(compat, "optimizable_components", lambda d: {"description": "d", "body": "THEBODY"})
    monkeypatch.setattr(compat, "_llm", lambda model: model)   # pass the model name through as the "llm"
    # the skill arm serves THEBODY (score 0.9); the no-skill baseline does not (0.2)
    monkeypatch.setattr(compat, "_score",
                        lambda llm, system, task, role="compat": 0.9 if "THEBODY" in system else 0.2)

    out = compat.run_compat("tailwind", log=lambda *a: None)
    assert set(out["models"]) == {"m1", "m2"}
    m1 = out["models"]["m1"]
    assert m1["skill_mean"] == pytest.approx(0.9)
    assert m1["baseline_mean"] == pytest.approx(0.2)
    assert m1["lift"] == pytest.approx(0.7)
    assert out["tasks"] == 2
    written = json.loads((tmp_path / "out" / "tailwind.json").read_text())
    assert written["skill"] == "tailwind" and set(written["models"]) == {"m1", "m2"}


def test_baseline_arm_is_cached_across_runs(tmp_path, monkeypatch):
    """The no-skill baseline does not depend on the skill body, so a re-sweep after editing a skill
    must not pay for it twice — that was half the cost of every run after the first."""
    (tmp_path / "tailwind").mkdir()
    (tmp_path / "tailwind" / "SKILL.md").write_text("x")
    monkeypatch.setattr(compat, "resolve_skill_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(compat, "COMPAT_DIR", tmp_path / "out")
    monkeypatch.setattr(compat, "BASELINE_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("COMPAT_MODELS", "m1")
    holdout = [{"task": "t", "rubric": "r"}]
    monkeypatch.setattr(compat, "load_tasks", lambda skill: ([], holdout, {}))
    monkeypatch.setattr(compat, "_llm", lambda model: model)

    served = []

    def fake_score(llm, system, task, role="compat"):
        served.append("skill" if "THEBODY" in system else "baseline")
        return 0.9 if "THEBODY" in system else 0.2

    monkeypatch.setattr(compat, "optimizable_components", lambda d: {"description": "d", "body": "THEBODY"})
    monkeypatch.setattr(compat, "_score", fake_score)

    first = compat.run_compat("tailwind", log=lambda *a: None)
    assert served == ["skill", "baseline"]

    served.clear()
    second = compat.run_compat("tailwind", log=lambda *a: None)
    assert served == ["skill"], "the baseline arm was re-run instead of reused"
    assert second["models"]["m1"]["baseline_mean"] == first["models"]["m1"]["baseline_mean"]


def test_compat_serves_the_local_model_from_the_local_endpoint(monkeypatch):
    """AGENT_MODEL is served by this box's own endpoint — a free row in the grid. Every other slug
    is hosted. Routing the whole sweep through one endpoint is what forced a by-hand override."""
    monkeypatch.setenv("AGENT_MODEL", "dot-backbone")
    monkeypatch.setenv("MODEL_BASE_URL", "http://local:8011/v1")
    monkeypatch.setenv("BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("MODEL_API_KEY", "local-key")
    monkeypatch.setenv("API_KEY", "hosted-key")

    assert str(compat._llm("dot-backbone").openai_api_base) == "http://local:8011/v1"
    assert str(compat._llm("anthropic/claude-sonnet-4.5").openai_api_base) == "https://openrouter.ai/api/v1"


def test_run_compat_rejects_unknown_skill(tmp_path, monkeypatch):
    """An unresolvable name is a roots misconfiguration, and the message has to say so — the old
    text pointed at skills/, a directory the operator may never have configured."""
    monkeypatch.setattr("mcp_server.registry.load_skills", lambda *a, **k: [])
    with pytest.raises(SystemExit, match="SKILL_ROUTER_PATHS"):
        compat.run_compat("nope", log=lambda *a: None)
