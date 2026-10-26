"""Unit tests for the continuous loop's health-gating (mine + run_ab are mocked)."""
from optimize import loop as L


def test_loop_skips_healthy_skills(monkeypatch):
    monkeypatch.setattr(L, "mine", lambda skill, log=print: {"mean_score": 0.9, "traces": 5})
    called = []
    def fake_call_1(skill, **k):
        called.append(skill)
        return {}

    monkeypatch.setattr(L, "run_ab", fake_call_1)
    res = L.loop(["pdf"])
    assert res["pdf"]["optimized"] is False and called == []   # healthy -> not optimized


def test_loop_defers_decision_while_mining_clusters_remain_unjudged(monkeypatch):
    monkeypatch.setattr(L, "mine", lambda skill, log=print: {
        "mean_score": 0.9, "traces": 100, "coverage": 0.8, "health_complete": False})
    called = []
    def fake_call_2(skill, **kwargs):
        called.append(skill)
        return {}

    monkeypatch.setattr(L, "run_ab", fake_call_2)

    result = L.loop(["pdf"])

    assert result["pdf"]["reason"] == "mining_backlog"
    assert result["pdf"]["optimized"] is False
    assert called == []


def test_loop_optimizes_failing_skills(monkeypatch):
    monkeypatch.setattr(L, "mine", lambda skill, log=print: {"mean_score": 0.3, "traces": 5})
    monkeypatch.setattr(L, "run_ab", lambda skill, **k: {"improved": True, "gate": {"promotable": True}})
    res = L.loop(["pdf"])
    assert res["pdf"]["optimized"] is True


def test_loop_forces_optimize_even_if_healthy(monkeypatch):
    monkeypatch.setattr(L, "mine", lambda skill, log=print: {"mean_score": 0.95, "traces": 5})
    monkeypatch.setattr(L, "run_ab", lambda skill, **k: {"improved": False, "gate": {"promotable": False}})
    res = L.loop(["pdf"], force=True)
    assert res["pdf"]["optimized"] is True


def test_loop_optimizes_when_no_trace_signal(monkeypatch):
    def _no_traces(skill, log=print):
        raise SystemExit("no traces")
    monkeypatch.setattr(L, "mine", _no_traces)
    monkeypatch.setattr(L, "run_ab", lambda skill, **k: {"improved": True, "gate": {"promotable": True}})
    res = L.loop(["pdf"])
    assert res["pdf"]["optimized"] is True   # no signal -> optimize rather than assume healthy


def test_loop_runs_description_pass_when_configured(monkeypatch):
    monkeypatch.setattr(L, "mine", lambda skill, log=print: {"mean_score": 0.3, "traces": 5})
    order = []
    monkeypatch.setattr(L, "PASSES", ["body", "description"])
    def fake_call_3(skill, **k):
        order.append('body')
        return {'improved': False, 'gate': {'promotable': False}}

    monkeypatch.setattr(L, "run_ab",
                        fake_call_3)
    import optimize.routing as routing_mod
    def fake_call_4(skill, **k):
        order.append('description')
        return {'improved': True, 'gate': {'promotable': True}}

    monkeypatch.setattr(routing_mod, "run_routing",
                        fake_call_4)
    res = L.loop(["pdf"])
    assert order == ["body", "description"]                      # greedy, in order
    assert res["pdf"]["gate"]["promotable"] is True              # the promotable pass surfaces
    assert res["pdf"]["improved"] is True


def test_loop_rejects_unknown_pass(monkeypatch):
    import pytest
    monkeypatch.setattr(L, "mine", lambda skill, log=print: {"mean_score": 0.1, "traces": 5})
    monkeypatch.setattr(L, "PASSES", ["body", "bodyy"])
    with pytest.raises(SystemExit, match="bodyy"):
        L.loop(["pdf"])
