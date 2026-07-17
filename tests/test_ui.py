"""Approval-UI endpoint guards: key preflight, slug validation, same-origin check, pending lifecycle."""
import pytest
from fastapi.testclient import TestClient

from optimize import promote as P
from ui.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    return TestClient(app)


def test_optimize_without_key_is_friendly_400(client, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    r = client.post("/api/optimize/pdf")
    assert r.status_code == 400
    assert "OPENROUTER_API_KEY" in r.json()["detail"]
    assert ".env" in r.json()["detail"]


def test_optimize_rejects_invalid_skill_name(client):
    r = client.post("/api/optimize/Not_A_Slug")
    assert r.status_code == 400
    assert "invalid skill name" in r.json()["detail"]


def test_optimize_without_task_set_is_404(client):
    r = client.post("/api/optimize/no-such-skill")
    assert r.status_code == 404


def test_cross_origin_post_refused(client):
    r = client.post("/api/optimize/pdf", headers={"origin": "http://evil.example"})
    assert r.status_code == 403


def test_same_origin_post_allowed_past_guard(client):
    host = "testserver"
    r = client.post("/api/optimize/no-such-skill", headers={"origin": f"http://{host}", "host": host})
    assert r.status_code == 404  # past the origin guard, fails on the later task-set check


def test_pending_unknown_skill_is_404(client):
    assert client.get("/api/pending/pdf").status_code == 404


def test_promote_without_pending_is_404(client):
    assert client.post("/api/promote/pdf").status_code == 404


def test_promote_blocked_gate_is_409(client):
    P.save_pending("pdf", {"skill": "pdf", "gate": {"promotable": False, "blocked": ["regression"]},
                           "champion_components": {}, "challenger_components": {}})
    r = client.post("/api/promote/pdf")
    assert r.status_code == 409


def test_reject_discards_pending(client):
    P.save_pending("pdf", {"skill": "pdf", "champion_components": {}, "challenger_components": {}})
    assert P.load_pending("pdf") is not None
    assert client.post("/api/reject/pdf").status_code == 200
    assert P.load_pending("pdf") is None


def test_skills_list_empty_library(client):
    r = client.get("/api/skills")
    assert r.status_code == 200
    assert r.json() == []


def test_pending_renders_component_diff_and_warnings(client):
    P.save_pending("pdf", {
        "skill": "pdf", "dataset": "pdf-holdout",
        "gepa": {"seed_score": 0.1, "best_score": 0.9, "budget": 30},
        "ab": {"champion": {"run": 1, "mean": 0.2, "scores": [0.2], "tokens": {}},
               "challenger": {"run": 2, "mean": 0.8, "scores": [0.8], "tokens": {}}},
        "gate": {"promotable": True, "blocked": [], "warnings": ["challenger drops 90% of the champion body"]},
        "changed_components": ["body"],
        "champion_components": {"description": "d", "body": "old line"},
        "challenger_components": {"description": "d", "body": "new line"},
    })
    p = client.get("/api/pending/pdf").json()
    assert p["changed"] == ["SKILL.md (body)"]
    assert "-old line" in p["diff"] and "+new line" in p["diff"]
    assert "SKILL.md (body) (champion)" in p["diff"]
    assert p["gate"]["warnings"] == ["challenger drops 90% of the champion body"]


def test_promote_passes_through_result(client, monkeypatch):
    import ui.app as ui_app
    P.save_pending("pdf", {"skill": "pdf", "gate": {"promotable": True, "blocked": []},
                           "champion_components": {}, "challenger_components": {}})
    monkeypatch.setattr(ui_app, "promote", lambda skill: f"promoted '{skill}'")
    r = client.post("/api/promote/pdf")
    assert r.status_code == 200 and r.json() == {"result": "promoted 'pdf'"}


def test_cross_origin_promote_and_reject_refused(client):
    for endpoint in ("/api/promote/pdf", "/api/reject/pdf"):
        assert client.post(endpoint, headers={"origin": "http://evil.example"}).status_code == 403


def test_config_reports_langfuse_url(client, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_URL", "http://example.test:3100")
    monkeypatch.delenv("CARN_DIR", raising=False)
    assert client.get("/api/config").json() == {"langfuse_url": "http://example.test:3100",
                                                "carn_enabled": False}


def test_runs_empty_by_default(client):
    assert client.get("/api/runs").json() == {}


def test_pending_routing_pass_renders_without_ab(client):
    P.save_pending("pdf", {
        "skill": "pdf", "kind": "routing", "dataset": "pdf-routing",
        "gepa": {"seed_score": 0.6, "best_score": 1.0, "budget": 60},
        "routing": {"champion": {"top1": 0.5, "recall_at_3": 0.5, "no_route_precision": 1.0},
                    "challenger": {"top1": 1.0, "recall_at_3": 1.0, "no_route_precision": 1.0},
                    "parity": {"rate": 1.0, "total": 2}},
        "gate": {"promotable": True, "blocked": [], "warnings": []},
        "changed_components": ["description"],
        "champion_components": {"description": "old trigger", "body": "b"},
        "challenger_components": {"description": "new trigger", "body": "b"},
    })
    p = client.get("/api/pending/pdf").json()
    assert p["kind"] == "routing" and p["ab"] is None
    assert p["routing"]["challenger"]["top1"] == 1.0
    assert "-old trigger" in p["diff"] and "+new trigger" in p["diff"]


def test_optimize_surfaces_pin_conflicts_as_400(client, monkeypatch):
    import optimize
    def conflict():
        raise SystemExit("error: provider pin conflicts detected before spending any tokens:\n  MODEL=x: nope")
    monkeypatch.setattr(optimize, "preflight_provider_pins", conflict)
    r = client.post("/api/optimize/pdf")
    assert r.status_code == 400 and "pin conflicts" in r.json()["detail"]
