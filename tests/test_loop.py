"""Unit tests for the continuous loop's health-gating (mine + run_ab are mocked)."""
from optimize import loop as L


def test_loop_skips_healthy_skills(monkeypatch):
    monkeypatch.setattr(L, "mine", lambda skill, log=print: {"mean_score": 0.9, "traces": 5})
    called = []
    monkeypatch.setattr(L, "run_ab", lambda skill, **k: called.append(skill) or {})
    res = L.loop(["pdf"])
    assert res["pdf"]["optimized"] is False and called == []   # healthy -> not optimized


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
