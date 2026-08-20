"""Change-control UI guards: key preflight, slug validation, same-origin check, the pending
lifecycle, and the history/rollback surface."""
import json
import threading
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from ingot.mcp_server import registry
from ingot.optimize import harbor_report
from ingot.optimize import promote as P
from ingot.optimize import publication as Q
from ui import app as A
from ui.app import app


class _Layout(HTMLParser):
    """Which elements each id sits inside, from the parsed page.

    The board's own behavior has no test harness (no browser runner is in this repo), so a claim
    about where an element lives is checked against the parsed tree rather than a substring that
    a reformat would break."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
            "param", "source", "track", "wbr"}

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self._open: list[str | None] = []
        self.ancestors: dict[str, list[str]] = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        node = dict(attrs).get("id")
        if node:
            self.ancestors[node] = [a for a in self._open if a]
        self._open.append(node)

    def handle_endtag(self, tag):
        if tag not in self.VOID and self._open:
            self._open.pop()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from ui import auth
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "no-auth.json")  # auth off unless a test opts in
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    return TestClient(app)


def test_optimize_without_key_is_friendly_400(client, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    r = client.post("/api/optimize/pdf")
    assert r.status_code == 400
    assert "API_KEY" in r.json()["detail"]
    assert ".env" in r.json()["detail"]


def test_optimize_rejects_invalid_skill_name(client):
    r = client.post("/api/optimize/Not_A_Slug")
    assert r.status_code == 400
    assert "invalid skill name" in r.json()["detail"]


def test_optimize_without_task_set_is_404(client):
    r = client.post("/api/optimize/no-such-skill")
    assert r.status_code == 404


def _eval_skill(tmp_path, monkeypatch, name="mounted-skill"):
    import ui.app as U
    from ingot.mcp_server.registry import write_skill_md

    root = tmp_path / "read-only-library"
    skill = root / name
    skill.mkdir(parents=True)
    write_skill_md(skill / "SKILL.md", {"name": name, "description": "Mounted description."},
                   "Mounted body.")
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(root))
    monkeypatch.setattr(U, "TASKS_DIR", tasks)
    U.RUNS.clear()
    return tasks


def _run_threads_inline(monkeypatch):
    import ui.app as U

    class InlineThread:
        def __init__(self, target, args=(), **_kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(U.threading, "Thread", InlineThread)


def test_create_eval_set_reads_an_indexed_skill_and_persists_the_draft(
        client, tmp_path, monkeypatch):
    tasks = _eval_skill(tmp_path, monkeypatch)
    _run_threads_inline(monkeypatch)

    def draft(name, description, body, tasks_dir, log):
        assert (name, description, body) == (
            "mounted-skill", "Mounted description.", "Mounted body.")
        assert tasks_dir.parent == tasks
        (tasks_dir / f"{name}.yaml").write_text(
            "skill: mounted-skill\ntrain:\n- task: train\nholdout:\n- task: holdout\n")
        log("[draft] wrote eval set")
        return tasks_dir / f"{name}.yaml"

    monkeypatch.setattr("ingot.optimize.draft.draft_and_save", draft)
    response = client.post("/api/evals/mounted-skill")

    assert response.status_code == 200
    assert response.json() == {"started": "mounted-skill"}
    assert (tasks / "mounted-skill.yaml").exists()
    assert client.get("/api/runs").json()["mounted-skill"] == {
        "status": "done", "action": "eval", "log": ["[draft] wrote eval set"]}


def test_create_eval_set_refuses_to_overwrite_an_existing_set(client, tmp_path, monkeypatch):
    tasks = _eval_skill(tmp_path, monkeypatch)
    (tasks / "mounted-skill.yaml").write_text("keep: me\n")

    response = client.post("/api/evals/mounted-skill")

    assert response.status_code == 409
    assert "already has" in response.json()["detail"]
    assert (tasks / "mounted-skill.yaml").read_text() == "keep: me\n"


def test_create_eval_set_preserves_a_file_created_while_drafting(
        client, tmp_path, monkeypatch):
    tasks = _eval_skill(tmp_path, monkeypatch)
    _run_threads_inline(monkeypatch)

    def racing_draft(name, _description, _body, tasks_dir, log):
        staged = tasks_dir / f"{name}.yaml"
        staged.write_text("draft: mine\n")
        (tasks / f"{name}.yaml").write_text("draft: theirs\n")
        return staged

    monkeypatch.setattr("ingot.optimize.draft.draft_and_save", racing_draft)

    assert client.post("/api/evals/mounted-skill").status_code == 200
    run = client.get("/api/runs").json()["mounted-skill"]
    assert run["status"] == "error"
    assert run["log"] == ["ERROR: 'mounted-skill' already has an eval task set"]
    assert (tasks / "mounted-skill.yaml").read_text() == "draft: theirs\n"


def test_create_eval_set_shares_the_paid_run_lock(client, tmp_path, monkeypatch):
    _eval_skill(tmp_path, monkeypatch)
    import ui.app as U
    U.RUNS["other-skill"] = {"status": "running", "action": "optimize", "log": []}

    response = client.post("/api/evals/mounted-skill")

    assert response.status_code == 409
    assert "already in progress" in response.json()["detail"]


def test_create_eval_set_surfaces_background_errors(client, tmp_path, monkeypatch):
    tasks = _eval_skill(tmp_path, monkeypatch)
    _run_threads_inline(monkeypatch)

    def partial_draft(name, _description, _body, tasks_dir, log):
        (tasks_dir / f"{name}.yaml").write_text("partial: true\n")
        raise RuntimeError("teacher down")

    monkeypatch.setattr("ingot.optimize.draft.draft_and_save", partial_draft)

    assert client.post("/api/evals/mounted-skill").status_code == 200
    run = client.get("/api/runs").json()["mounted-skill"]
    assert run["status"] == "error"
    assert run["action"] == "eval"
    assert run["log"] == ["ERROR: teacher down"]
    assert not (tasks / "mounted-skill.yaml").exists()


def test_create_eval_set_requires_an_indexed_skill_and_provider(client, tmp_path, monkeypatch):
    import ui.app as U
    U.RUNS.clear()
    assert client.post("/api/evals/not-indexed").status_code == 404

    _eval_skill(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    response = client.post("/api/evals/mounted-skill")
    assert response.status_code == 400
    assert "API_KEY" in response.json()["detail"]


def test_eval_set_api_exposes_the_measured_inputs(client, tmp_path, monkeypatch):
    tasks = _eval_skill(tmp_path, monkeypatch)
    (tasks / "mounted-skill.yaml").write_text(
        "skill: mounted-skill\n"
        "train:\n"
        "- task: Write the train answer.\n"
        "  rubric: Include the train result.\n"
        "  checklist:\n"
        "  - id: train_result\n"
        "    criterion: Includes the train result.\n"
        "    weight: 3\n"
        "    dimension: correctness\n"
        "holdout:\n"
        "- task: Write the held-out answer.\n"
        "  rubric: Include the held-out result.\n"
        "routing:\n"
        "- task: Use the mounted skill.\n"
        "  expected: mounted-skill\n"
        "acceptance:\n"
        "- id: no_placeholder\n"
        "  forbid: TODO\n"
        "  description: No placeholder output.\n")

    response = client.get("/api/evals/mounted-skill")

    assert response.status_code == 200
    assert response.json() == {
        "skill": "mounted-skill",
        "train": [{
            "task": "Write the train answer.",
            "rubric": "Include the train result.",
            "checklist": [{
                "id": "train_result",
                "criterion": "Includes the train result.",
                "weight": 3,
                "dimension": "correctness",
            }],
        }],
        "holdout": [{
            "task": "Write the held-out answer.",
            "rubric": "Include the held-out result.",
        }],
        "routing": [{
            "task": "Use the mounted skill.",
            "expected": "mounted-skill",
        }],
        "acceptance": [{
            "id": "no_placeholder",
            "forbid": "TODO",
            "description": "No placeholder output.",
        }],
        "leakage": False,
        "counts": {"train": 1, "holdout": 1, "routing": 1, "acceptance": 1, "checks": 1},
    }


def test_eval_set_api_marks_legacy_flat_tasks_as_leaky(client, tmp_path, monkeypatch):
    tasks = _eval_skill(tmp_path, monkeypatch)
    (tasks / "mounted-skill.yaml").write_text(
        "tasks:\n- task: Same task trains and gates.\n"
        "  checklist:\n  - criterion: Produce the requested result.\n")

    response = client.get("/api/evals/mounted-skill")

    assert response.status_code == 200
    assert response.json()["leakage"] is True
    assert response.json()["train"] == response.json()["holdout"]
    assert response.json()["counts"]["checks"] == 1


def test_eval_set_api_reports_missing_and_malformed_files(client, tmp_path, monkeypatch):
    tasks = _eval_skill(tmp_path, monkeypatch)
    assert client.get("/api/evals/mounted-skill").status_code == 404
    (tasks / "mounted-skill.yaml").write_text("train: [")
    response = client.get("/api/evals/mounted-skill")
    assert response.status_code == 503
    assert "eval set is unreadable" in response.json()["detail"]


def test_review_api_runs_existing_reviewer_and_serves_persisted_result(
        client, tmp_path, monkeypatch):
    tasks = _eval_skill(tmp_path, monkeypatch)
    (tasks / "mounted-skill.yaml").write_text(
        "train:\n- task: train\nholdout:\n- task: holdout\n")
    _run_threads_inline(monkeypatch)
    import ingot.optimize.review as review_module
    review_dir = tmp_path / "reviews"
    monkeypatch.setattr(review_module, "REVIEW_DIR", review_dir)

    def review(skill, log):
        assert skill == "mounted-skill"
        result = {
            "skill": skill, "model": "test-model", "revision": "abc123",
            "score": 0.75, "tasks": 2, "checks": 4, "failed_checks": 1,
            "by_dimension": {"correctness": 1.0},
            "findings": [{"task": "holdout", "check": "answer", "criterion": "Answer it.",
                          "weight": 2, "dimension": "correctness", "value": 0.5,
                          "note": "Missing detail.", "cost": 1.0}],
            "per_task": [{"task": "train", "score": 1.0}, {"task": "holdout", "score": 0.5}],
        }
        review_dir.mkdir()
        (review_dir / f"{skill}.json").write_text(__import__("json").dumps(result))
        log("[review] complete")
        return result

    monkeypatch.setattr(review_module, "run_review", review)

    response = client.post("/api/reviews/mounted-skill")

    assert response.status_code == 200
    assert response.json() == {"started": "mounted-skill"}
    assert client.get("/api/runs").json()["mounted-skill"] == {
        "status": "done", "action": "review", "log": ["[review] complete"]}
    saved = client.get("/api/reviews/mounted-skill")
    assert saved.status_code == 200
    assert saved.json()["score"] == 0.75
    assert saved.json()["failed_checks"] == 1
    assert saved.json()["created"] > 0

    persisted = __import__("json").loads(
        (review_dir / "mounted-skill.json").read_text())
    persisted["created"] = 123
    (review_dir / "mounted-skill.json").write_text(__import__("json").dumps(persisted))
    assert client.get("/api/reviews/mounted-skill").json()["created"] == 123


@pytest.mark.parametrize(("recorded", "message"), [
    (None, "did not record a skill revision"),
    ("old-revision", "active skill changed since this review ran"),
])
def test_review_api_marks_unbound_or_changed_results_stale(
        client, tmp_path, monkeypatch, recorded, message):
    _eval_skill(tmp_path, monkeypatch)
    import ingot.optimize.review as review_module
    review_dir = tmp_path / "reviews"
    review_dir.mkdir()
    monkeypatch.setattr(review_module, "REVIEW_DIR", review_dir)
    (review_dir / "mounted-skill.json").write_text(json.dumps({
        "skill": "mounted-skill", "revision": recorded, "score": 1.0,
    }))

    result = client.get("/api/reviews/mounted-skill")

    assert result.status_code == 200
    assert message in result.json()["stale"]


def test_review_api_accepts_the_revision_recorded_by_the_review_path(
        client, tmp_path, monkeypatch):
    from ingot.mcp_server.registry import skill_revision
    _eval_skill(tmp_path, monkeypatch)
    import ingot.optimize.review as review_module
    review_dir = tmp_path / "reviews"
    review_dir.mkdir()
    monkeypatch.setattr(review_module, "REVIEW_DIR", review_dir)
    skill = tmp_path / "read-only-library" / "mounted-skill"
    (review_dir / "mounted-skill.json").write_text(json.dumps({
        "skill": "mounted-skill", "revision": skill_revision(skill), "score": 1.0,
    }))

    result = client.get("/api/reviews/mounted-skill")

    assert result.status_code == 200
    assert result.json()["stale"] is None


def test_review_api_requires_evals_and_shares_the_paid_run_lock(
        client, tmp_path, monkeypatch):
    tasks = _eval_skill(tmp_path, monkeypatch)
    assert client.post("/api/reviews/mounted-skill").status_code == 404
    (tasks / "mounted-skill.yaml").write_text("train:\n- task: train\n")
    import ui.app as U
    U.RUNS["other-skill"] = {"status": "running", "action": "optimize", "log": []}
    response = client.post("/api/reviews/mounted-skill")
    assert response.status_code == 409
    assert "already in progress" in response.json()["detail"]


def test_review_result_reports_missing_and_malformed_files(client, tmp_path, monkeypatch):
    _eval_skill(tmp_path, monkeypatch)
    import ingot.optimize.review as review_module
    review_dir = tmp_path / "reviews"
    review_dir.mkdir()
    monkeypatch.setattr(review_module, "REVIEW_DIR", review_dir)
    assert client.get("/api/reviews/mounted-skill").status_code == 404
    (review_dir / "mounted-skill.json").write_text("{")
    response = client.get("/api/reviews/mounted-skill")
    assert response.status_code == 503
    assert "review result is unreadable" in response.json()["detail"]


def test_cross_origin_post_refused(client):
    r = client.post("/api/optimize/pdf", headers={"origin": "http://evil.example"})
    assert r.status_code == 403


def test_same_origin_post_allowed_past_guard(client):
    host = "testserver"
    r = client.post("/api/optimize/no-such-skill", headers={"origin": f"http://{host}", "host": host})
    assert r.status_code == 404  # past the origin guard, fails on the later task-set check


def test_pending_unknown_skill_is_404(client):
    assert client.get("/api/pending/pdf").status_code == 404


def test_pending_creation_appears_as_to_be_added_and_can_be_reviewed(client, tmp_path,
                                                                     monkeypatch):
    import ui.app as U
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(tmp_path / "empty-library"))
    components = {"description": "Write conversion copy.", "body": "Body"}
    revision = registry.skill_revision(registry.writable_skill_dir("copywriting"), components)
    P.save_pending("copywriting", {
        "skill": "copywriting", "kind": "creation", "created": 7,
        "champion_components": {},
        "challenger_components": components,
        "changed_components": ["description", "body"],
        "gate": {"promotable": True, "blocked": [], "kind": "new_skill_admission"},
        "evidence": {"challenger": {"revision": revision}},
        "creation": {"summary": "Add vetted copywriting skill."},
    })
    U._SKILLS_CACHE = None
    U._SKILLS_CACHE_KEY = None

    skills = client.get("/api/skills").json()
    assert skills == [{"name": "copywriting", "description": "Write conversion copy.",
                       "has_tasks": False, "pending": True, "revision": "",
                       "uses": 0, "publishing": False, "provenance": "proposed",
                       "status": None, "active": False}]
    pending = client.get("/api/pending/copywriting")
    assert pending.status_code == 200
    assert pending.json()["kind"] == "creation"
    assert pending.json()["stale"] is None

    html = client.get("/").text
    assert "to be added" in html
    assert "Review addition" in html
    assert "function creationEvidence(" in html
    assert 'p.creation ? "Approve & add"' in html
    assert '["proposed", "To be added"' in html
    assert 'p.creation ? "Confirm addition, "' in html
    assert "Submission requirements met; evidence is operator-supplied" in html
    assert "const activeSkills = skills.filter(skill => skill.active !== false)" in html


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


def test_reject_of_a_missing_pending_is_404(client):
    assert P.load_pending("pdf") is None
    assert client.post("/api/reject/pdf").status_code == 404


def test_reject_records_a_reject_audit_entry(client):
    """A rejection is a decision the trail has to show, with the challenger revision when the
    pending record carries one (approve/rollback already audit; reject used to write nothing)."""
    P.save_pending("pdf", {"skill": "pdf", "champion_components": {}, "challenger_components": {},
                           "evidence": {"challenger": {"revision": "abc123"}}})
    assert client.post("/api/reject/pdf").status_code == 200
    trail = client.get("/api/history").json()["audit"]["records"]
    assert [r["action"] for r in trail] == ["reject"]
    assert trail[0]["skill"] == "pdf" and trail[0]["revision"] == "abc123"


def test_reject_records_an_optional_normalized_reason(client):
    P.save_pending("pdf", {"skill": "pdf", "champion_components": {},
                           "challenger_components": {}})
    r = client.post("/api/reject/pdf", json={"reason": "  deleted   required checks\nby mistake  "})
    assert r.status_code == 200
    record = client.get("/api/history").json()["audit"]["records"][0]
    assert record["action"] == "reject"
    assert record["reason"] == "deleted required checks by mistake"


def test_reject_reason_is_limited_before_pending_is_consumed(client):
    P.save_pending("pdf", {"skill": "pdf", "champion_components": {},
                           "challenger_components": {}})
    assert client.post("/api/reject/pdf", json={"reason": "x" * 501}).status_code == 422
    assert P.load_pending("pdf") is not None


def test_reject_audits_an_empty_revision_when_none_is_recorded(client):
    P.save_pending("pdf", {"skill": "pdf", "champion_components": {}, "challenger_components": {}})
    assert client.post("/api/reject/pdf").status_code == 200
    trail = client.get("/api/history").json()["audit"]["records"]
    assert [r["action"] for r in trail] == ["reject"] and trail[0]["revision"] == ""


def test_double_reject_is_404_and_not_double_audited(client):
    """Validate+delete+audit run under change_control: a second reject of an already-discarded
    change must 404, never re-audit and return 200 (the TOCTOU the lock closes)."""
    P.save_pending("pdf", {"skill": "pdf", "champion_components": {}, "challenger_components": {},
                           "evidence": {"challenger": {"revision": "abc123"}}})
    assert client.post("/api/reject/pdf").status_code == 200
    assert client.post("/api/reject/pdf").status_code == 404
    trail = client.get("/api/history").json()["audit"]["records"]
    assert [r["action"] for r in trail] == ["reject"]   # exactly one, not two


def test_reject_is_refused_while_another_change_is_in_flight(client, monkeypatch):
    """reject holds the same one-at-a-time lock as promote/rollback."""
    import ui.app as U
    P.save_pending("pdf", {"skill": "pdf", "champion_components": {}, "challenger_components": {}})
    assert U.CHANGE_LOCK.acquire(blocking=False)
    try:
        r = client.post("/api/reject/pdf")
    finally:
        U.CHANGE_LOCK.release()
    assert r.status_code == 409 and "already in progress" in r.json()["detail"]
    assert P.load_pending("pdf") is not None   # refused, not consumed


def test_auth_me_is_a_200_unauthenticated_shape_in_password_mode(client):
    """The frontend polls /auth/me on every load; the default (non-OIDC) config must answer 200
    with a stable shape rather than 404."""
    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.json() == {"authenticated": False, "email": "", "name": "", "role": ""}


def test_auth_me_surfaces_the_password_user(client, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "password")
    monkeypatch.setenv("AUTH_USER", "reviewer")
    monkeypatch.setenv("AUTH_PASSWORD", "secret")
    r = client.get("/auth/me", auth=("reviewer", "secret"))
    assert r.status_code == 200
    assert r.json() == {"authenticated": True, "email": "", "name": "reviewer",
                        "role": "admin"}


def test_compose_mcp_service_mounts_the_runs_directory():
    """The mcp service records skill usage into runs/skill_usage.json; without the runs mount that
    write stays inside the ephemeral container and the board always reads 0 uses."""
    import yaml
    from pathlib import Path
    compose = yaml.safe_load((Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text())
    assert "./runs:/app/runs" in compose["services"]["mcp"]["volumes"]


def test_compose_mcp_and_ui_share_the_pending_queue_uid():
    """MCP creation proposals and UI reviews share mode-0600 files in runs/pending."""
    import yaml
    from pathlib import Path
    compose = yaml.safe_load((Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text())

    assert compose["services"]["mcp"]["user"] == compose["services"]["ui"]["user"]


def test_skills_list_empty_library(client):
    r = client.get("/api/skills")
    assert r.status_code == 200
    assert r.json() == []


def test_pending_renders_component_diff_and_warnings(client):
    P.save_pending("pdf", {
        "skill": "pdf", "dataset": "pdf-holdout",
        "inner_loop": {"seed_score": 0.1, "best_score": 0.9},
        "ab": {"champion": {"run": 1, "mean": 0.2, "scores": [0.2], "tokens": {}},
               "challenger": {"run": 2, "mean": 0.8, "scores": [0.8], "tokens": {}}},
        "evidence_paths": {"json": "/app/runs/evidence/pdf/1/evidence.json",
                           "markdown": "/app/runs/evidence/pdf/1/EVIDENCE.md"},
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
    assert p["inner_loop"] == {"seed_score": 0.1, "best_score": 0.9}
    assert p["evidence"]["markdown"].endswith("EVIDENCE.md")


def test_pending_reports_large_body_change_risk_and_side_by_side_content(client):
    before = "\n".join(f"line {number}" for number in range(1, 9))
    P.save_pending("pdf", {
        "skill": "pdf", "changed_components": ["body"],
        "champion_components": {"description": "d", "body": before},
        "challenger_components": {"description": "d", "body": "line 1"},
    })
    p = client.get("/api/pending/pdf").json()
    assert p["risk"] == {"added_lines": 0, "removed_lines": 7, "changed_pct": 87.5,
                          "size_delta_pct": p["risk"]["size_delta_pct"], "high_risk": True}
    assert p["comparison"] == [{"component": "SKILL.md (body)", "before": before,
                                 "after": "line 1"}]


def test_skill_version_explorer_reads_active_pending_and_snapshot(client, tmp_path, monkeypatch):
    root = tmp_path / "skills"
    active = root / "pdf"
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: Active description.\n---\nactive body\n")
    (active / "notes.md").write_text("active notes")
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(root))

    pending = {"description": "Pending description.", "body": "pending body",
               "file:notes.md": "pending notes"}
    P.save_pending("pdf", {"skill": "pdf", "created": 123,
                            "champion_components": {"description": "Active description.",
                                                    "body": "active body"},
                            "challenger_components": pending})

    snapshot = P.revisions_dir() / "pdf" / "abc123"
    snapshot.mkdir(parents=True)
    (snapshot / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: Snapshot description.\n---\nsnapshot body\n")
    (snapshot / "notes.md").write_text("snapshot notes")

    versions = client.get("/api/skills/pdf/versions").json()["versions"]
    assert [version["kind"] for version in versions] == ["active", "pending", "snapshot"]
    assert client.get("/api/skills/pdf/versions/active").json()["body"] == "active body"
    pending_payload = client.get("/api/skills/pdf/versions/pending").json()
    assert pending_payload["description"] == "Pending description."
    assert pending_payload["files"] == [{"path": "notes.md", "content": "pending notes"}]
    snapshot_payload = client.get("/api/skills/pdf/versions/abc123").json()
    assert snapshot_payload["body"] == "snapshot body"
    assert snapshot_payload["files"] == [{"path": "notes.md", "content": "snapshot notes"}]


def test_skill_version_explorer_refuses_unknown_versions(client, tmp_path, monkeypatch):
    root = tmp_path / "skills"
    skill = root / "pdf"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: pdf\ndescription: PDF.\n---\nbody\n")
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(root))
    assert client.get("/api/skills/pdf/versions/missing").status_code == 404


def test_pending_exposes_model_and_judge_for_the_comparison_panel(client):
    P.save_pending("pdf", {
        "skill": "pdf", "model": "qwen/qwen3-32b", "judge": "google/gemini-2.5-flash",
        "champion_components": {"description": "d", "body": "a"},
        "challenger_components": {"description": "d", "body": "b"},
        "changed_components": ["body"],
        "ab": {"champion": {"mean": 0.2, "scores": [0.2], "tokens": {"mean_output": 100, "mean_input": 200}},
               "challenger": {"mean": 0.8, "scores": [0.8], "tokens": {"mean_output": 90, "mean_input": 180}}}})
    p = client.get("/api/pending/pdf").json()
    assert p["model"] == "qwen/qwen3-32b" and p["judge"] == "google/gemini-2.5-flash"
    # the panel reads model/judge, both means, per-task scores, and before/after tokens
    assert p["ab"]["challenger"]["tokens"]["mean_output"] == 90
    assert p["ab"]["challenger"]["tokens"]["mean_input"] == 180
    assert p["ab"]["champion"]["scores"] == [0.2] and p["ab"]["challenger"]["scores"] == [0.8]


def test_comparison_panel_controls_are_in_the_page(client):
    """The review action opens evidence, then the modal's primary button makes the decision."""
    html = client.get("/").text
    layout = _Layout(html)
    for element_id in ("cmp-overlay", "cmp-body", "cmp-approve", "cmp-cancel"):
        assert element_id in layout.ancestors, f"comparison panel missing #{element_id}"
    assert "cmp-overlay" in layout.ancestors["cmp-approve"]
    assert "cmp-confirm" not in layout.ancestors
    assert '$("#cmp-approve").onclick = () => act("#cmp-msg"' in html


def test_review_panel_ships_risk_side_diff_and_confirmed_rejection(client):
    html = client.get("/").text
    layout = _Layout(html)
    for element_id in ("risk-summary", "side-diff", "side-diff-body", "reject-overlay",
                       "reject-reason", "reject-confirm"):
        assert element_id in layout.ancestors, f"review panel missing #{element_id}"
    assert "pending-card" in layout.ancestors["risk-summary"]
    assert "pending-card" in layout.ancestors["side-diff"]
    assert "reject-overlay" in layout.ancestors["reject-confirm"]
    assert "renderRisk(p.risk);" in html
    assert "renderSideDiff(p.comparison);" in html
    assert 'JSON.stringify({reason: $("#reject-reason").value})' in html


def test_skill_list_ships_search_filters_version_explorer_and_live_updates(client):
    html = client.get("/").text
    layout = _Layout(html)
    for element_id in ("skill-filter", "skill-filter-count", "skill-overlay",
                       "skill-version", "skill-file", "board-announcer"):
        assert element_id in layout.ancestors, f"skill explorer missing #{element_id}"
    assert 'id="skill-search"' in html  # input is a void element, so _Layout does not record it
    assert 'aria-live="polite"' in html
    assert "signature !== boardSignature" in html
    assert "skillInventory.filter" in html
    assert "/api/skills/${encodeURIComponent(skill)}/versions" in html
    assert "versionCount" in html and "active, pending, and snapshotted versions" in html
    assert "renderSkills(skills, runs || runInventory, hist)" in html


def test_skill_families_filter_the_skill_list_and_category_atlas(client):
    html = client.get("/").text
    layout = _Layout(html)

    assert "skill-family-filter" in layout.ancestors
    assert 'aria-label="Filter atlas by skill family"' in html
    assert "All families" in html
    assert "function selectFamily(" in html
    assert "function familyMatches(" in html
    assert "familyMatches(s.name)" in html
    assert "d.clusters.map((cluster, index) => ({cluster, index}))" in html
    assert '$("#cluster-chips").innerHTML = "";' in html
    assert "clustersAttempted" in html
    assert "select.disabled = !clusterData?.clusters?.length" in html
    assert '$("#nav-clusters").textContent = "0";' in html
    assert '$("#skill-family-filter").onchange' in html


def test_trace_inventory_api_never_returns_answers(client, tmp_path, monkeypatch):
    import ui.app as ui_app

    store = tmp_path / "local-traces.json"
    store.write_text(json.dumps({
        "schema_version": "ingot/local-traces/v1",
        "generated_at": 1785254400,
        "traces": [{
            "id": "trace-1", "timestamp": "2026-07-28T10:00:00Z", "harness": "claude",
            "task": "Review the interface", "answer": "private answer",
            "skills": [{"name": "saas-interface-review", "revision": None}],
            "tags": ["skill:saas-interface-review"], "usage": {"input_tokens": 10,
                                                                 "output_tokens": 4},
        }],
    }))
    monkeypatch.setattr(ui_app, "LOCAL_TRACE_FILE", store)

    response = client.get("/api/traces")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert "task" not in payload["recent"][0]
    assert "answer" not in payload["recent"][0]
    assert "private answer" not in response.text

    preview = client.get("/api/traces?include_tasks=true")
    assert preview.json()["recent"][0]["task"] == "Review the interface"
    assert "private answer" not in preview.text

    filtered = client.get("/api/traces?project=other&since=2026-07-28")
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 0
    assert client.get("/api/traces?since=not-a-date").status_code == 400


def test_trace_inventory_is_a_routed_console_view(client):
    html = client.get("/").text
    layout = _Layout(html)

    assert "traces-section" in layout.ancestors
    assert "trace-summary" in layout.ancestors
    assert "trace-list" in layout.ancestors
    assert 'data-route="traces"' in html
    assert "j(traceUrl())" in html
    assert 'id="trace-project"' in html
    assert 'id="trace-since"' in html
    assert 'id="trace-task-previews"' in html
    assert 'notation: "compact"' in html


def test_comparison_panel_orders_tokens_and_tables_numbered_task_scores(client):
    html = client.get("/").text
    compare = html[html.index("function buildCompare(p)"):html.index("function openCompare()")]

    assert compare.index("<td>input</td>") < compare.index("<td>output</td>")
    assert "<th>before</th><th>after</th><th>Δ</th>" in compare
    assert "<td>Task ${i + 1}</td>" in compare
    task_count = "Math.max(beforeScores.length, afterScores.length)"
    assert f"const scoreRows = Array.from({{length: {task_count}}}" in compare
    assert 'class="cmp-pertask"' not in compare


def test_api_skills_rows_carry_a_load_count(client, monkeypatch, tmp_path):
    """Every active skill row exposes `uses` so the UI can render the load-counter chip."""
    import ui.app as ui_app
    from ingot.mcp_server import usage_counts

    class _Skill:
        name, description, revision = "pdf", "merge PDFs", "rev1"
        root = str(tmp_path / "library" / "pdf")   # provenance classifies from the skill's own root
    monkeypatch.setattr(ui_app, "load_skills", lambda: [_Skill()])
    monkeypatch.setattr(usage_counts, "load_counts", lambda: {"pdf": 7})
    active = client.get("/api/skills").json()
    assert active and active[0]["uses"] == 7


def test_pending_without_search_scores_still_renders(client):
    P.save_pending("pdf", {
        "skill": "pdf", "champion_components": {"description": "d", "body": "a"},
        "challenger_components": {"description": "d", "body": "b"},
        "changed_components": ["body"],
    })
    assert client.get("/api/pending/pdf").json()["inner_loop"] is None


def test_pending_exposes_retrospective_evidence(client):
    P.save_pending("pdf", {
        "skill": "pdf", "kind": "retrospective",
        "champion_components": {"description": "d", "body": "a"},
        "challenger_components": {"description": "d", "body": "b"},
        "changed_components": ["body"],
        "retrospective": {
            "summary": "Repeated omission.", "trigger": "Two matching failures.",
            "minimal_content": "Add the missing guard.", "producer": "skill-retrospective",
            "caller": "build-loop", "evidence": ["run one", "run two"],
            "pressure_scenario": "A rushed repair.", "risk": "May slow simple work.",
            "verification": {"status": "passed", "command": "pytest", "result": "passed"},
        },
    })

    payload = client.get("/api/pending/pdf").json()
    assert payload["kind"] == "retrospective"
    assert payload["retrospective"]["producer"] == "skill-retrospective"
    assert payload["retrospective"]["verification"]["status"] == "passed"


def test_index_renders_retrospective_evidence_on_both_decision_surfaces(client):
    html = client.get("/").text
    assert "function retrospectiveEvidence(" in html
    assert "p.retrospective" in html
    assert "Pressure scenario" in html
    assert "Verification" in html
    assert "border: 1px solid transparent; overflow: hidden;" in html


def test_promote_passes_through_result(client, monkeypatch):
    import ui.app as ui_app
    P.save_pending("pdf", {"skill": "pdf", "gate": {"promotable": True, "blocked": []},
                           "champion_components": {}, "challenger_components": {}})
    monkeypatch.setattr(ui_app, "approve_pending", lambda skill, actor="?": f"promoted '{skill}'")
    r = client.post("/api/promote/pdf")
    assert r.status_code == 200 and r.json() == {"result": "promoted 'pdf'"}


def test_pending_exposes_approved_publication_and_disables_repeat_approval(client):
    pending = {
        "skill": "pdf", "gate": {"promotable": True, "blocked": []},
        "champion_components": {"description": "Merge PDFs.", "body": "old"},
        "challenger_components": {"description": "Merge PDFs.", "body": "new"},
        "evidence": {"champion": {"revision": "a" * 64},
                     "challenger": {"revision": "b" * 64}},
    }
    P.save_pending("pdf", pending)
    Q.queue_publication("pdf", pending, "admin", "promote")

    payload = client.get("/api/pending/pdf").json()
    assert payload["publication"]["state"] == "approved_publishing"
    html = client.get("/").text
    assert "Approved · publishing to vault" in html
    assert "p.publication" in html
    assert '$("#approve").disabled' in html


def test_pending_does_not_attach_a_receipt_from_an_older_proposal(client):
    old = {
        "skill": "pdf", "kind": "retrospective",
        "champion_components": {"description": "Merge PDFs.", "body": "old"},
        "challenger_components": {"description": "Merge PDFs.", "body": "first change"},
        "evidence": {"champion": {"revision": "a" * 64},
                     "challenger": {"revision": "b" * 64}},
        "retrospective": {"proposal_id": "old-proposal"},
    }
    receipt = Q.queue_publication("pdf", old, "admin", "promote")
    Q.update_publication(receipt.id, state="active")
    P.save_pending("pdf", {
        **old,
        "challenger_components": {"description": "Merge PDFs.", "body": "second change"},
        "evidence": {"champion": {"revision": "b" * 64},
                     "challenger": {"revision": "c" * 64}},
        "retrospective": {"proposal_id": "new-proposal"},
    })

    payload = client.get("/api/pending/pdf").json()

    assert payload["publication"] is None


def _queued(skill: str, **changes):
    pending = {
        "skill": skill, "gate": {"promotable": True, "blocked": []},
        "champion_components": {"description": "Merge PDFs.", "body": "old"},
        "challenger_components": {"description": "Merge PDFs.", "body": "new"},
        "evidence": {"champion": {"revision": "a" * 64},
                     "challenger": {"revision": "b" * 64}},
    }
    receipt = Q.queue_publication(skill, pending, "admin", "promote")
    return Q.update_publication(receipt.id, **changes) if changes else receipt


def _proposed(monkeypatch, tmp_path, skill="copywriting"):
    """A creation pending, which is what the board surfaces when no library is indexed."""
    import ui.app as U
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(tmp_path / "empty-library"))
    P.save_pending(skill, {
        "skill": skill, "kind": "creation",
        "champion_components": {"description": "", "body": ""},
        "challenger_components": {"description": "Write conversion copy.", "body": "Body"},
        "gate": {"promotable": True, "blocked": [], "kind": "new_skill_admission"},
        "creation": {"summary": "Add vetted copywriting skill."},
    })
    U._SKILLS_CACHE = None
    U._SKILLS_CACHE_KEY = None


def test_skills_listing_separates_publishing_from_awaiting_review(client, tmp_path, monkeypatch):
    """Approval does not free the review slot, so an approved change stays `pending`. Without a
    second flag the board counts it as still awaiting a decision the reviewer already made — which
    is what made an approved creation sit in `To be added` looking untouched."""
    _proposed(monkeypatch, tmp_path)
    before = {s["name"]: s for s in client.get("/api/skills").json()}["copywriting"]
    _queued("copywriting", state="awaiting_merge", pr=9)

    after = {s["name"]: s for s in client.get("/api/skills").json()}["copywriting"]

    assert (before["pending"], before["publishing"]) == (True, False)
    assert (after["pending"], after["publishing"]) == (True, True)


def test_a_finished_publication_stops_marking_its_skill_as_publishing(client, tmp_path, monkeypatch):
    """Only the newest receipt counts, or an earlier attempt would pin the skill to `publishing`."""
    _proposed(monkeypatch, tmp_path)
    _queued("copywriting", state="active", pr=9)

    listed = {s["name"]: s for s in client.get("/api/skills").json()}["copywriting"]

    assert listed["publishing"] is False


def test_publications_lane_is_empty_before_anything_is_approved(client):
    payload = client.get("/api/publications").json()

    assert payload["publications"] == []
    assert payload["unreadable"] is None
    assert "awaiting_merge" in payload["live_states"]


def test_publications_lane_survives_the_pending_record_it_came_from(client, monkeypatch):
    """The whole point of the lane: `publication_for_skill` needs a pending record, and approval
    consumes it, so once a change is approved the console could no longer see it travelling."""
    monkeypatch.setenv("INGOT_FORGE_REPOSITORY", "someone/skills")
    _queued("pdf", state="awaiting_merge", pr=9)

    payload = client.get("/api/publications").json()

    assert [r["skill"] for r in payload["publications"]] == ["pdf"]
    assert payload["publications"][0]["state"] == "awaiting_merge"
    assert payload["publications"][0]["pr_url"] == "https://github.com/someone/skills/pull/9"


def test_publications_lane_omits_a_pull_request_url_when_there_is_no_pull_request(client):
    _queued("pdf")

    assert client.get("/api/publications").json()["publications"][0]["pr_url"] is None


def test_publications_lane_links_no_pull_request_under_the_local_backend(client, monkeypatch):
    """Only the forge backend has a pull request, and only it knows the repository. Linking to a
    hardcoded one produced a 404 for every deployment that was not the author's."""
    monkeypatch.delenv("INGOT_FORGE_REPOSITORY", raising=False)
    _queued("pdf", state="awaiting_merge", pr=9)

    assert client.get("/api/publications").json()["publications"][0]["pr_url"] is None


def test_publications_lane_reports_a_receipt_store_it_cannot_read(client, monkeypatch):
    """`Path.glob` swallows `PermissionError`, so an unreadable store returns an empty list —
    indistinguishable from a quiet lane, which is the reading a stalled publisher most invites."""
    _queued("pdf", state="awaiting_merge", pr=9)
    monkeypatch.setattr(A.os, "access", lambda *a, **k: False)

    payload = client.get("/api/publications").json()

    assert payload["publications"] == []
    assert "cannot read the receipt store" in payload["unreadable"]


def test_pending_names_the_pull_request_a_stalled_publication_waits_on(client):
    """A vault that cannot auto-merge leaves the receipt waiting on a person. Without the pull
    request number on the card, the reviewer has no way to learn they are what it waits for."""
    pending = {
        "skill": "pdf", "gate": {"promotable": True, "blocked": []},
        "champion_components": {"description": "Merge PDFs.", "body": "old"},
        "challenger_components": {"description": "Merge PDFs.", "body": "new"},
        "evidence": {"champion": {"revision": "a" * 64},
                     "challenger": {"revision": "b" * 64}},
    }
    P.save_pending("pdf", pending)
    receipt = Q.queue_publication("pdf", pending, "admin", "promote")
    Q.update_publication(receipt.id, state="awaiting_merge", pr=4, auto_merge=False,
                         note="auto-merge unavailable, waiting on a human merge: denied")

    payload = client.get("/api/pending/pdf").json()

    assert payload["publication"]["pr"] == 4
    assert "waiting on a human merge" in payload["publication"]["note"]
    assert "merge vault PR #" in client.get("/").text


def test_cross_origin_promote_and_reject_refused(client):
    for endpoint in ("/api/promote/pdf", "/api/reject/pdf"):
        assert client.post(endpoint, headers={"origin": "http://evil.example"}).status_code == 403


def test_config_reports_langfuse_url(client, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_URL", "http://example.test:3100")
    assert client.get("/api/config").json() == {"langfuse_url": "http://example.test:3100"}


def test_runs_empty_by_default(client):
    assert client.get("/api/runs").json() == {}


def test_pending_routing_pass_renders_without_ab(client):
    P.save_pending("pdf", {
        "skill": "pdf", "kind": "routing", "dataset": "pdf-routing",
        "inner_loop": {"seed_score": 0.6, "best_score": 1.0, "budget": 60},
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
    import ingot.optimize as optimize
    def conflict():
        raise SystemExit("error: provider pin conflicts detected before spending any tokens:\n  MODEL=x: nope")
    monkeypatch.setattr(optimize, "preflight_provider_pins", conflict)
    r = client.post("/api/optimize/pdf")
    assert r.status_code == 400 and "pin conflicts" in r.json()["detail"]


def test_skills_api_reports_eval_status_for_all_skills(client, tmp_path, monkeypatch):
    # the UI's evals chip keys off has_tasks, every skill must carry it, task set or not
    from ingot.mcp_server import registry
    from ingot.mcp_server.registry import write_skill_md
    import ui.app as U
    for name in ("with-evals", "without-evals"):
        d = registry.library_dir() / name   # hermetic per-test root (conftest)
        d.mkdir(parents=True)
        write_skill_md(d / "SKILL.md", {"name": name, "description": "d"}, "b")
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "with-evals.yaml").write_text("train: []")
    monkeypatch.setattr(U, "TASKS_DIR", tasks)
    flags = {s["name"]: s["has_tasks"] for s in client.get("/api/skills").json()}
    assert flags == {"with-evals": True, "without-evals": False}


def test_index_ships_eval_creation_for_skills_without_tasks(client):
    html = client.get("/").text
    assert "no evals" in html
    assert "has_tasks" in html
    assert "Create eval set" in html
    assert "createEvalSet" in html and "/api/evals/" in html
    assert "auto-drafts" not in html
    assert "Optimize with SkillOpt" in html


def test_index_surfaces_incomplete_eval_coverage_not_only_zero_coverage(client):
    html = client.get("/").text
    assert "EVAL COVERAGE" in html
    assert "without an eval task set" in html
    assert "withTasks.length < activeSkills.length" in html


def test_index_labels_eval_drafting_separately_from_optimization(client):
    html = client.get("/").text
    assert 'run?.action === "eval"' in html
    assert 'const runLabel = drafting ? "Eval draft" : reviewing ? "Current-skill review" : "SkillOpt"' in html
    assert "${runLabel} ${esc(run.status" in html
    assert "Drafting eight train/holdout tasks" in html


def test_index_exposes_eval_inputs_and_current_skill_review(client):
    html = client.get("/").text
    layout = _Layout(html)
    for element_id in ("skill-evals", "skill-eval-summary", "skill-eval-groups",
                       "skill-review", "skill-review-run", "skill-eval-msg"):
        assert "skill-overlay" in layout.ancestors[element_id]
    assert "/api/evals/" in html
    assert "/api/reviews/" in html
    assert "Run review" in html
    assert "Train tasks" in html and "Held-out tasks" in html
    assert "Routing cases" in html and "Acceptance rules" in html
    assert "Failed checks" in html


def test_index_labels_review_runs_separately_from_drafting_and_optimization(client):
    html = client.get("/").text
    assert 'run?.action === "review"' in html
    assert '"Current-skill review"' in html
    assert "Review can take a minute" in html


def test_index_leads_with_review_before_candidate_generation(client):
    """Reviewers must meet the evidence and the decision first; generation is downstream."""
    html = client.get("/").text
    assert html.index('id="review-section"') < html.index('id="history-section"')
    assert html.index('id="history-section"') < html.index('id="skills"')
    assert 'id="run-section"' not in html
    assert "Release control for agent skills" in html
    assert "change control" in html and "skill optimizer" not in html


def test_index_nests_each_skillopt_log_under_its_skill(client):
    html = client.get("/").text
    assert "runInventory[s.name]" in html
    assert 'class="skill-run-log"' in html
    assert 'role="log"' in html and 'aria-live="polite"' in html
    assert "renderSkills(skills, runs || runInventory, hist)" in html
    assert "Optimization can take a few minutes" in html
    assert 'id="log"' not in html


def test_carn_viewer_is_gone(client):
    """The optional CARN integration was removed outright: no page, no routes, no config flag.

    The checks name what was removed. A bare `carn` substring over the whole page also matched any
    prose that happens to contain those four letters, so it failed for reasons that had nothing to
    do with the integration."""
    routes = ("/carn", "/api/carn/overview", "/api/carn/graphs", "/api/carn/runs", "/api/carn/trie")
    for path in routes:
        assert client.get(path).status_code == 404
    assert not [p for p in client.get("/openapi.json").json()["paths"] if "carn" in p.lower()]
    html = client.get("/").text
    for removed in (*routes, "carn.html", "carnUrl", "carn_url"):
        assert removed not in html
    assert "carn_url" not in client.get("/api/config").json()


def _promoted_skill(tmp_path, monkeypatch):
    """An active skill with one approved promotion behind it, so a snapshot exists to restore."""
    from ingot.mcp_server.registry import optimizable_components, skill_revision
    root = tmp_path / "skills"
    skill = root / "pdf"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: pdf\ndescription: Merge PDFs.\n---\napproved body\n")
    monkeypatch.setenv("INGOT_LIBRARY", str(root))
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(root))
    champion = optimizable_components(skill)
    challenger = {**champion, "body": "promoted body"}
    from ingot.mcp_server.registry import load_skills
    current = load_skills(root)[0]
    gate = {"promotable": True, "blocked": []}
    P.save_pending("pdf", {
        "skill": "pdf", "gate": gate,
        "champion_components": champion, "challenger_components": challenger,
        "evidence": {"champion": {"revision": current.revision},
                     "challenger": {"revision": skill_revision(skill, challenger)},
                     "gate": gate},
    })
    P._activate_approved("pdf", P.load_pending("pdf"))
    return skill, current.revision


def test_history_lists_rollback_targets_and_audit_trail(client, tmp_path, monkeypatch):
    skill, replaced = _promoted_skill(tmp_path, monkeypatch)
    history = client.get("/api/history").json()
    assert [r["revision"] for r in history["revisions"]["pdf"]] == [replaced]
    assert history["revisions"]["pdf"][0]["created"] > 0  # labels the option a reviewer picks
    assert [r["action"] for r in history["audit"]["records"]] == ["approve"]
    assert history["audit"]["records"][0]["skill"] == "pdf"
    assert history["audit"]["total"] == 1
    assert "body" not in str(history)  # metadata only: the trail never carries skill text


def test_history_is_empty_before_any_promotion(client):
    assert client.get("/api/history").json() == {"revisions": {},
                                                 "audit": {"records": [], "total": 0}}


def test_history_does_not_rescan_the_skill_library(client, tmp_path, monkeypatch):
    """The skills listing already hashes every skill on each 3s poll. History reads the snapshot
    store instead, so one refresh does not pay for two full library scans.

    The counter patches the registry's own library scan, which `load_skills` looks up at call time:
    counting `ui.app.load_skills` would have missed a rescan reached through any other module's
    import of it, and passed whether or not history scanned anything."""
    from ingot.mcp_server import registry
    _promoted_skill(tmp_path, monkeypatch)
    real_sources = registry.skill_sources
    scans = []
    monkeypatch.setattr(registry, "skill_sources",
                        lambda root: scans.append(root) or real_sources(root))

    history = client.get("/api/history").json()

    assert scans == []
    assert [r["revision"] for r in history["revisions"]["pdf"]]
    assert client.get("/api/skills").status_code == 200
    assert scans, "the counter must catch the scan the skills listing does pay for"


def test_history_survives_an_unreadable_approval_trail(client, tmp_path, monkeypatch):
    """One broken store must not blank the other: rollback targets still render."""
    import ui.app as U
    _promoted_skill(tmp_path, monkeypatch)
    monkeypatch.setattr(U, "read_audit", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))

    history = client.get("/api/history").json()

    assert history["audit"] == {"records": [], "total": 0}
    assert [r["revision"] for r in history["revisions"]["pdf"]]


def test_history_survives_an_unreadable_snapshot_store(client, tmp_path, monkeypatch):
    """The reverse direction: naming the snapshot store raises before any per-skill listing is
    reached, and the approval trail is the half that has to survive it."""
    import ui.app as U
    _promoted_skill(tmp_path, monkeypatch)
    monkeypatch.setattr(U, "list_snapshotted_skills",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))

    history = client.get("/api/history").json()

    assert history["revisions"] == {}
    assert [r["action"] for r in history["audit"]["records"]] == ["approve"]
    assert history["audit"]["total"] == 1


def test_history_survives_one_unreadable_skill_snapshot_directory(client, tmp_path, monkeypatch):
    """A single unlistable skill drops out of the picker; the others and the trail still render."""
    import ui.app as U
    _promoted_skill(tmp_path, monkeypatch)

    def fail(name):
        raise OSError("nope")

    monkeypatch.setattr(U, "list_revisions", fail)
    history = client.get("/api/history").json()

    assert history["revisions"] == {}
    assert [r["action"] for r in history["audit"]["records"]] == ["approve"]


def test_rollback_queues_a_snapshot_for_vault_publication(client, tmp_path, monkeypatch):
    """History rollback takes the same Git lane as approval: it reports publication, and the
    served skill only changes once the vault merge lands."""
    skill, replaced = _promoted_skill(tmp_path, monkeypatch)
    assert "promoted body" in (skill / "SKILL.md").read_text()

    r = client.post(f"/api/rollback/pdf/{replaced}")

    assert r.status_code == 200 and "publishing to vault" in r.json()["result"]
    assert "promoted body" in (skill / "SKILL.md").read_text()
    record = Q.publication_for_skill("pdf")
    assert record["action"] == "rollback" and record["candidate_revision"] == replaced
    trail = client.get("/api/history").json()["audit"]["records"]
    assert [a["action"] for a in trail] == ["approve"]


def test_rollback_rejects_unknown_revision_and_bad_names(client, tmp_path, monkeypatch):
    _promoted_skill(tmp_path, monkeypatch)
    assert client.post("/api/rollback/pdf/deadbeef").status_code == 404
    assert client.post("/api/rollback/Not_A_Slug/deadbeef").status_code == 400


def test_rollback_refuses_a_traversing_revision_at_the_application(client, tmp_path, monkeypatch):
    """A `..` segment must be refused by revision validation, not merely missed by the router:
    the same string reaching ingot.optimize.promote directly has to be rejected there too."""
    _promoted_skill(tmp_path, monkeypatch)

    r = client.post("/api/rollback/pdf/%2E%2E", follow_redirects=False)
    assert r.status_code == 404
    assert "invalid revision" in r.json()["detail"]

    with pytest.raises(ValueError, match="invalid revision"):
        P.rollback("pdf", "../../etc")
    with pytest.raises(ValueError, match="invalid revision"):
        P.rollback("pdf", "sub/dir")


def test_cross_origin_rollback_refused(client):
    r = client.post("/api/rollback/pdf/abc", headers={"origin": "http://evil.example"})
    assert r.status_code == 403


# --- one change-control action at a time ------------------------------------------------------

def _promotable_pending() -> None:
    P.save_pending("pdf", {"skill": "pdf", "gate": {"promotable": True, "blocked": []},
                           "champion_components": {}, "challenger_components": {}})


def test_approval_and_rollback_are_refused_while_one_is_in_flight(client, tmp_path, monkeypatch):
    """Promotion and rollback each snapshot, stage, and swap directories over several steps, and
    the endpoints run on a thread pool. A second action is refused with 409 rather than allowed to
    interleave those steps, the way a second SkillOpt run is."""
    import ui.app as U
    skill, replaced = _promoted_skill(tmp_path, monkeypatch)
    _promotable_pending()

    assert U.CHANGE_LOCK.acquire(blocking=False)
    try:
        promote = client.post("/api/promote/pdf")
        roll = client.post(f"/api/rollback/pdf/{replaced}")
    finally:
        U.CHANGE_LOCK.release()

    assert promote.status_code == 409 and "already in progress" in promote.json()["detail"]
    assert roll.status_code == 409 and "already in progress" in roll.json()["detail"]
    assert "promoted body" in (skill / "SKILL.md").read_text()   # neither one swapped anything
    assert P.load_pending("pdf") is not None                     # and the review slot survives


def test_a_second_promotion_is_refused_while_the_first_is_still_swapping(client, tmp_path,
                                                                        monkeypatch):
    """The guard has to wrap the work, not just the entry check: the second request arrives while
    the first is inside `approve_pending`, which is exactly when the two would interleave."""
    import ui.app as U
    _promoted_skill(tmp_path, monkeypatch)
    _promotable_pending()
    entered, release, first = threading.Event(), threading.Event(), {}

    def slow_approve(skill, actor="?"):
        entered.set()
        release.wait(10)
        return f"promoted '{skill}'"

    monkeypatch.setattr(U, "approve_pending", slow_approve)
    worker = threading.Thread(target=lambda: first.update(r=client.post("/api/promote/pdf")))
    worker.start()
    try:
        assert entered.wait(10), "the first promotion never reached the guarded section"
        second = client.post("/api/promote/pdf")
    finally:
        release.set()
        worker.join(10)

    assert second.status_code == 409 and "already in progress" in second.json()["detail"]
    assert first["r"].status_code == 200
    assert U.CHANGE_LOCK.acquire(blocking=False), "the guard must release on the way out"
    U.CHANGE_LOCK.release()


# --- stale evidence ---------------------------------------------------------------------------

def _stale_pending(tmp_path, monkeypatch):
    """A review slot whose champion has since been edited on disk."""
    from ingot.mcp_server.registry import load_skills, optimizable_components, skill_revision
    root = tmp_path / "skills"
    skill = root / "pdf"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: pdf\ndescription: Merge PDFs.\n---\nreviewed body\n")
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(root))
    champion = optimizable_components(skill)
    challenger = {**champion, "body": "proposed body"}
    current = load_skills(root)[0]
    gate = {"promotable": True, "blocked": []}
    P.save_pending("pdf", {
        "skill": "pdf", "gate": gate, "changed_components": ["body"],
        "champion_components": champion, "challenger_components": challenger,
        "evidence": {"champion": {"revision": current.revision},
                     "challenger": {"revision": skill_revision(skill, challenger)}, "gate": gate},
    })
    (skill / "SKILL.md").write_text("---\nname: pdf\ndescription: Merge PDFs.\n---\nedited elsewhere\n")
    return skill


def test_pending_reports_stale_evidence_before_approval(client, tmp_path, monkeypatch):
    """The card is the decision surface: a champion that moved on disk has to be refused there,
    not after the reviewer commits to an approval."""
    _stale_pending(tmp_path, monkeypatch)
    p = client.get("/api/pending/pdf").json()
    assert "champion revision changed" in p["stale"]
    assert p["gate"]["promotable"] is True   # the gate passed; freshness is the separate check


def test_promote_with_stale_evidence_is_409(client, tmp_path, monkeypatch):
    skill = _stale_pending(tmp_path, monkeypatch)
    r = client.post("/api/promote/pdf")
    assert r.status_code == 409
    assert "champion revision changed" in r.json()["detail"]
    assert "edited elsewhere" in (skill / "SKILL.md").read_text()
    assert P.load_pending("pdf") is not None   # refused, not consumed


def test_pending_is_not_stale_for_a_fresh_change(client, tmp_path, monkeypatch):
    from ingot.mcp_server.registry import load_skills, optimizable_components, skill_revision
    root = tmp_path / "skills"
    skill = root / "pdf"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: pdf\ndescription: Merge PDFs.\n---\nreviewed body\n")
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(root))
    champion = optimizable_components(skill)
    challenger = {**champion, "body": "proposed body"}
    gate = {"promotable": True, "blocked": []}
    P.save_pending("pdf", {
        "skill": "pdf", "gate": gate, "changed_components": ["body"],
        "champion_components": champion, "challenger_components": challenger,
        "evidence": {"champion": {"revision": load_skills(root)[0].revision},
                     "challenger": {"revision": skill_revision(skill, challenger)}, "gate": gate},
    })
    assert client.get("/api/pending/pdf").json()["stale"] is None


# --- evidence bundle API ----------------------------------------------------------------------

def _evidence_bundle(monkeypatch, tmp_path, recorded=None, body="# Behavioral Skill CI: pdf\n"):
    """A pending record pointing at a bundle inside a private runs/evidence root."""
    import ui.app as U
    evidence_root = (tmp_path / "runs" / "evidence").resolve()
    bundle = evidence_root / "pdf" / "1700000000"
    bundle.mkdir(parents=True)
    (bundle / "EVIDENCE.md").write_text(body)
    monkeypatch.setenv("INGOT_RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(U, "STATE_ROOT", tmp_path.resolve())
    P.save_pending("pdf", {
        "skill": "pdf", "champion_components": {}, "challenger_components": {},
        "evidence_paths": {"markdown": recorded or "runs/evidence/pdf/1700000000/EVIDENCE.md"},
    })
    return bundle


def test_evidence_returns_the_recorded_bundle(client, tmp_path, monkeypatch):
    _evidence_bundle(monkeypatch, tmp_path)
    r = client.get("/api/evidence/pdf")
    assert r.status_code == 200
    assert r.json()["markdown"].startswith("# Behavioral Skill CI: pdf")
    assert r.json()["path"] == "pdf/1700000000/EVIDENCE.md"


def test_evidence_reads_a_legacy_container_absolute_path(client, tmp_path, monkeypatch):
    """Bundles written before evidence locations became repo-relative recorded /app/... paths."""
    _evidence_bundle(monkeypatch, tmp_path,
                     recorded="/app/runs/evidence/pdf/1700000000/EVIDENCE.md")
    assert client.get("/api/evidence/pdf").status_code == 200


@pytest.mark.parametrize("recorded", [
    "runs/evidence/../../etc/passwd",
    "runs/evidence/pdf/../../../etc/passwd",
    "/etc/passwd",
    "/app/etc/passwd",
    "../outside.md",
])
def test_evidence_refuses_paths_outside_the_evidence_tree(client, tmp_path, monkeypatch, recorded):
    _evidence_bundle(monkeypatch, tmp_path, recorded=recorded)
    r = client.get("/api/evidence/pdf")
    assert r.status_code == 400
    assert "outside runs/evidence" in r.json()["detail"]


def test_evidence_refuses_a_symlink_out_of_the_evidence_tree(client, tmp_path, monkeypatch):
    bundle = _evidence_bundle(monkeypatch, tmp_path,
                              recorded="runs/evidence/pdf/1700000000/escape.md")
    secret = tmp_path / "secret.md"
    secret.write_text("do not serve this")
    (bundle / "escape.md").symlink_to(secret)
    r = client.get("/api/evidence/pdf")
    assert r.status_code == 400
    assert "do not serve this" not in r.text


def test_evidence_is_read_only(client, tmp_path, monkeypatch):
    _evidence_bundle(monkeypatch, tmp_path)
    for method in ("post", "put", "delete"):
        assert getattr(client, method)("/api/evidence/pdf").status_code == 405


def test_evidence_without_a_recorded_bundle_is_404(client):
    P.save_pending("pdf", {"skill": "pdf", "champion_components": {},
                           "challenger_components": {}})
    assert client.get("/api/evidence/pdf").status_code == 404


def test_evidence_for_an_unknown_skill_is_404_and_bad_names_are_400(client):
    assert client.get("/api/evidence/pdf").status_code == 404
    assert client.get("/api/evidence/Not_A_Slug").status_code == 400


def test_evidence_missing_file_is_404_not_a_server_error(client, tmp_path, monkeypatch):
    bundle = _evidence_bundle(monkeypatch, tmp_path)
    (bundle / "EVIDENCE.md").unlink()
    assert client.get("/api/evidence/pdf").status_code == 404


# --- review-surface copy ----------------------------------------------------------------------

def test_index_preserves_a_chosen_revision_across_refreshes(client):
    """The board re-polls every 3s; rebuilding the pickers reset the reviewer's choice."""
    html = client.get("/").text
    assert "historySignature" in html            # unchanged history is not rebuilt at all
    assert "#history select" in html             # and a rebuild carries the selection over
    assert "snapshotted ${esc(stamp(r.created))}" in html   # option labels carry the timestamp


def test_index_reports_action_failures(client):
    html = client.get("/").text
    assert 'act("#cmp-msg"' in html              # approve
    assert 'act("#reject-msg"' in html           # reject
    assert 'act("#history-msg"' in html          # rollback
    assert 'act("#skills-msg"' in html           # SkillOpt optimization
    assert "could not load history" in html      # a failed poll degrades per section
    assert "Promise.allSettled" in html


def test_index_keeps_the_action_result_visible_after_the_card_hides(client):
    """Approving or rejecting the last pending change hides the review card. The message that
    reports what happened has to sit outside the card, or the only confirmation a reviewer gets
    disappears with the element that was carrying it."""
    layout = _Layout(client.get("/").text)

    assert "review-section" in layout.ancestors["pending-msg"]
    assert "pending-card" not in layout.ancestors["pending-msg"]
    # the buttons stay in the card: they are only actionable while there is something to act on
    assert "pending-card" in layout.ancestors["approve"]
    assert "pending-card" in layout.ancestors["reject"]


def test_index_follows_the_queue_when_the_reviewed_card_is_gone(client):
    """A card whose slot was consumed (approved, rejected, or taken by another process) must not
    stay up with a live Approve button: the poll moves to the next quarantined change and carries
    the last action's result across."""
    html = client.get("/").text
    assert "if (skills) syncReviewCard(skills);" in html
    assert "!currentPending || !quarantined.includes(currentPending)" in html
    assert "showPending((undecided[0] ?? quarantined[0]), {keepMessage: true});" in html
    # An approved change keeps its pending record until the vault commit lands, so following the
    # queue must skip it rather than reopening a card whose decision is already made.
    assert "skills.filter(s => s.pending && !s.publishing).map(s => s.name)" in html
    assert "if (!quarantined.length) { showNoPending(); return; }" in html
    # a card opened by hand still clears the previous result
    assert "if (!keepMessage) say(\"#pending-msg\", \"\", false);" in html
    assert 'onclick="showPending(\'${esc(s.name)}\', {scroll: true})"' in html
    # Review is its own route now, so reaching it is a hash change rather than a scroll within one
    # long page. The card still has to name the skill whose button was clicked: a bare route change
    # would land on whichever change the queue happens to list first.
    assert 'if (scroll) {' in html and 'location.hash = "#/review"' in html


def test_index_renders_the_board_when_history_is_unavailable(client):
    """A failed history poll used to leave the KPI strip on its loading placeholder forever. The
    review state comes from the skills payload, so it renders either way, and the two numbers that
    do come from history say they are unavailable rather than reading as zero."""
    html = client.get("/").text
    assert "renderBoard(skills, skills.filter(s => s.has_tasks), hist);" in html
    assert "if (!history) {" in html
    assert 'kpi("", "–", "rollback points", "history unavailable")' in html
    assert 'kpi("", "–", "recorded decisions", "history unavailable")' in html
    assert "loading" in html                     # the placeholder the first paint starts from


def test_index_renders_the_evidence_bundle_and_blocks_stale_cards(client):
    html = client.get("/").text
    assert "/api/evidence/" in html
    assert "Evidence bundle" in html
    assert "Stale evidence" in html


def test_history_payload_is_byte_stable_between_polls(client, tmp_path, monkeypatch):
    """The board skips rebuilding the history rows when the payload is unchanged, which is what
    keeps a chosen revision selected across a 3s poll. That only holds if an unchanged store
    serializes identically: unordered iteration or a per-request timestamp would defeat it."""
    _promoted_skill(tmp_path, monkeypatch)
    first = client.get("/api/history").text
    assert client.get("/api/history").text == first
    assert client.get("/api/history").text == first


def test_history_orders_rollback_targets_newest_snapshot_first(client, tmp_path, monkeypatch):
    """The picker lists most-recently-snapshotted first, so option 0 is the change you just made."""
    from ingot.mcp_server.registry import load_skills, optimizable_components, skill_revision
    skill, first = _promoted_skill(tmp_path, monkeypatch)

    champion = optimizable_components(skill)
    challenger = {**champion, "body": "third body"}
    gate = {"promotable": True, "blocked": []}
    P.save_pending("pdf", {
        "skill": "pdf", "gate": gate,
        "champion_components": champion, "challenger_components": challenger,
        "evidence": {"champion": {"revision": load_skills(skill.parent)[0].revision},
                     "challenger": {"revision": skill_revision(skill, challenger)}, "gate": gate},
    })
    second = load_skills(skill.parent)[0].revision
    P._activate_approved("pdf", P.load_pending("pdf"))

    listed = [r["revision"] for r in client.get("/api/history").json()["revisions"]["pdf"]]
    assert listed == [second, first]


def test_publications_reports_a_quarantined_change_it_cannot_read(client):
    """list_pending skips an unreadable record so one corrupt file cannot break review, which also
    means an unreadable proposal is indistinguishable from no proposal and the board reports CLEAR
    over it. Observed live: the MCP container wrote records as root 0600 while the UI ran as uid
    1000, and an approved-and-waiting skill sat invisible for hours."""
    P.pending_dir().mkdir(parents=True, exist_ok=True)
    (P.pending_dir() / "measurement-integrity.json").write_bytes(b"\xff\xfe not json")

    payload = client.get("/api/publications").json()

    assert payload["pending_blocked"], "an unreadable quarantined change must be surfaced"
    assert "measurement-integrity.json" in payload["pending_blocked"]


def test_publications_stays_quiet_when_the_review_queue_is_readable(client):
    P.pending_dir().mkdir(parents=True, exist_ok=True)
    assert client.get("/api/publications").json()["pending_blocked"] is None


def test_a_non_utf8_pending_file_does_not_take_down_the_review_page(client):
    """list_pending caught OSError and JSONDecodeError but not UnicodeDecodeError, so a binary file
    in the queue raised straight through and the whole review surface 500ed."""
    P.pending_dir().mkdir(parents=True, exist_ok=True)
    (P.pending_dir() / "pdf.json").write_bytes(b"\xff\xfe\x00binary")
    assert client.get("/api/skills").status_code == 200


def test_harbor_matrix_is_served_for_a_skill_that_has_been_run(client, monkeypatch, tmp_path):
    """The console surface for the harness x model grid."""
    root = tmp_path / "harbor"
    root.mkdir()
    (root / "pdf.json").write_text(json.dumps({"judge": "google/gemini-2.5-flash", "harnesses": {
        "claude-code@anthropic/claude-opus-5": {
            "skill_mean": 0.75, "control_mean": 0.5, "lift": 0.25, "tasks_scored": 4,
            "tasks_dropped": [], "endpoint_url": "https://private.invalid/v1"},
        "aider@openai/gpt-5.5": {"error": "RuntimeError: every task returned an empty workspace"}}}))
    monkeypatch.setattr(harbor_report, "HARBOR_DIR", root)

    payload = client.get("/api/harbor/pdf").json()

    assert payload["measured"] == 1 and payload["unmeasured"] == 1
    rows = {r["combination"]: r for r in payload["rows"]}
    # The rule the whole surface exists for: a combination that did not run reaches the browser
    # with no lift key at all, so no renderer can put a number in its measurement column.
    assert "lift" not in rows["aider@openai/gpt-5.5"]
    assert rows["claude-code@anthropic/claude-opus-5"]["n"] == 4
    # A renderer may show the recorded alias/protocol, never an endpoint URL supplied by a run.
    assert "endpoint_url" not in rows["claude-code@anthropic/claude-opus-5"]
    assert client.get("/api/harbor").json()["skills"] == ["pdf"]


def test_harbor_page_pivots_sparse_evidence_by_harness_and_model(client):
    """The console gives every axis intersection its own evidence state, never an implied zero."""
    html = client.get("/").text

    # renderHarbor owns the pivot from API axes, rather than relying on a pre-filled rectangular
    # payload. The API deliberately sends only rows that were attempted.
    assert "function matrixCell(row)" in html
    assert "data.harnesses" in html and "data.models" in html
    assert "byHarness" in html and "byModel" in html
    # These loops are the rectangular matrix contract. Axis/map names alone would let a renderer
    # emit one header or skip sparse intersections, which silently changes absence into no cell.
    assert "${models.map(model => {" in html
    assert "const body = harnesses.map(harness =>" in html
    assert "models.map(model => matrixCell(byHarness.get(harness)?.get(model))).join(\"\")" in html
    assert "never run" in html
    assert "not measured" in html
    assert "toFixed(3)" in html
    assert "skill mean" in html and "control mean" in html
    assert "attempts" in html and "dropped" in html
    assert "target alias" in html and "protocol" in html and "error" in html
    # Measured and failed cells are buttons, so keyboard and pointer activation share one route;
    # a blank intersection is evidence of absence, not a neutral interactive result.
    assert 'class="mx-plate mx-measured' in html
    assert 'class="mx-plate mx-error"' in html
    assert "mx-blank" in html
    assert 'aria-label="never run"' in html
    assert "mx-details" in html and 'aria-live="polite"' in html


def test_harbor_matrix_css_contract_preserves_readable_sparse_columns(client):
    html = client.get("/").text

    assert ".mx-wrap" in html and "overflow-x: auto" in html
    assert ".mx-sticky" in html and "position: sticky" in html
    assert ".mx-corner" in html
    assert ".mx td, .mx th" in html and "min-width:" in html
    # Per-model warnings wrap inside a fixed evidence column; otherwise one ceiling warning can
    # stretch a sparse matrix across several screens.
    assert "table-layout: fixed" in html and "--mx-width" in html
    assert ".mx-plate:focus-visible" in html
    assert ".mx-blank" in html and ".mx-error" in html and ".mx-measured" in html


def test_harbor_page_plots_only_observed_model_scale_rows(client):
    html = client.get("/").text

    assert "function sizeLiftChart(rows)" in html
    assert "Math.log10" in html and "Number.isFinite(size)" in html and "size > 0" in html
    assert "sizeLiftChart(rows)" in html
    assert "data.legacy" not in html
    assert "data-size-point" in html and "generationShape" in html
    assert "Model size vs lift" in html and "Parameters (billions, log scale)" in html
    assert "Object.is(rounded, -0) ? 0 : rounded" in html
    assert "Failed and never-run cells remain in the evidence ledger below." in html
    assert 'tabindex="0"' in html and "event.key === \"Enter\"" in html
    assert "parameter_billions" in html and "quantization" in html and "tool parser" in html
    assert 'includes("Qwen3.5") ? "circle"' in html
    assert 'includes("Qwen3.6") ? "square" : "diamond"' in html
    assert "diamond = other model families" in html


def test_harbor_size_chart_keeps_endpoint_swarms_inside_the_plot(client):
    html = client.get("/").text

    assert "const swarmInset = Math.max(...sizes.map(size =>" in html
    assert "left + swarmInset" in html
    assert "plotRight - swarmInset" in html


def test_secondary_routes_put_their_own_job_first(client):
    html = client.get("/").text

    assert 'document.querySelector(".board-head").hidden = base !== "review"' in html
    assert ".board-head[hidden]" in html


def test_every_route_uses_the_evidence_cockpit_visual_system(client):
    """The full-console redesign must alter the shell, not only a chart inside the old page."""
    html = client.get("/").text

    assert 'class="topbar cockpit-command"' in html
    assert 'class="shell cockpit-shell"' in html
    assert 'class="sidebar cockpit-rail"' in html
    assert 'class="main cockpit-workspace"' in html
    assert "/* ---- evidence cockpit visual system ---- */" in html
    assert ".cockpit-rail .navlink.active::before" in html
    assert ".cockpit-workspace > .view:not([hidden])" in html
    assert ".cockpit-workspace .review-card" in html
    assert ".cockpit-workspace .trace-list" in html
    assert ".cockpit-workspace .mx-wrap" in html
    assert ".cockpit-shell { grid-template-columns: 1fr; }" in html
    assert ".cockpit-rail #nav-folders { display: contents; }" in html


def test_harbor_size_chart_has_a_persistent_readable_interaction_layer(client):
    html = client.get("/").text

    assert "mx-chart-stats" in html
    assert "mx-chart-legend" in html
    assert 'id="mx-chart-detail"' in html
    assert "pointOffset" in html and "* 18" in html
    assert "Measured evidence only" in html
    assert 'row.n == null ? "not recorded"' in html
    assert 'aria-live="polite"' in html


def test_harbor_poll_discovers_and_rerenders_progressive_results(client):
    html = client.get("/").text

    assert 'if (currentRoute() === "harnesses") await loadHarbor();' in html
    assert 'await j("/api/harbor")' in html
    assert "harborSkills = available" in html
    assert "const selected = harborSkill" in html


def test_a_skill_never_run_across_harnesses_is_a_404_not_an_empty_matrix(client, monkeypatch, tmp_path):
    """An empty grid on the page would read as 'no combination helps'. It has not been measured."""
    monkeypatch.setattr(harbor_report, "HARBOR_DIR", tmp_path / "harbor")
    response = client.get("/api/harbor/pdf")
    assert response.status_code == 404
    assert "harbor_eval" in response.json()["detail"]


def test_an_unreadable_matrix_is_an_error_not_a_silent_absence(client, monkeypatch, tmp_path):
    root = tmp_path / "harbor"
    root.mkdir()
    (root / "pdf.json").write_text("{ not json")
    monkeypatch.setattr(harbor_report, "HARBOR_DIR", root)
    assert client.get("/api/harbor/pdf").status_code == 503


def test_harbor_rejects_a_bad_skill_name(client):
    assert client.get("/api/harbor/..%2Fetc").status_code in (400, 404)
