"""Change-control UI guards: key preflight, slug validation, same-origin check, the pending
lifecycle, and the history/rollback surface."""
import threading
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from optimize import promote as P
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
    monkeypatch.setattr(P, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(P, "REVISIONS_DIR", tmp_path / "revisions")
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

    snapshot = P.REVISIONS_DIR / "pdf" / "abc123"
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
    """The two-step approve panel (Approve -> comparison -> final Approve -> Confirm) must ship in
    the served page, with the confirm button nested inside the modal overlay."""
    layout = _Layout(client.get("/").text)
    for element_id in ("cmp-overlay", "cmp-body", "cmp-approve", "cmp-confirm", "cmp-cancel"):
        assert element_id in layout.ancestors, f"comparison panel missing #{element_id}"
    assert "cmp-overlay" in layout.ancestors["cmp-confirm"]


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


def test_comparison_panel_orders_tokens_and_tables_numbered_task_scores(client):
    html = client.get("/").text
    compare = html[html.index("function buildCompare(p)"):html.index("function openCompare()")]

    assert compare.index("<td>input</td>") < compare.index("<td>output</td>")
    assert "<th>before</th><th>after</th><th>Δ</th>" in compare
    assert "<td>Task ${i + 1}</td>" in compare
    task_count = "Math.max(beforeScores.length, afterScores.length)"
    assert f"const scoreRows = Array.from({{length: {task_count}}}" in compare
    assert 'class="cmp-pertask"' not in compare


def test_api_skills_rows_carry_a_load_count(client, monkeypatch):
    """Every active skill row exposes `uses` so the UI can render the load-counter chip."""
    import ui.app as ui_app
    from mcp_server import usage_counts

    class _Skill:
        name, description, revision = "pdf", "merge PDFs", "rev1"
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


def test_promote_passes_through_result(client, monkeypatch):
    import ui.app as ui_app
    P.save_pending("pdf", {"skill": "pdf", "gate": {"promotable": True, "blocked": []},
                           "champion_components": {}, "challenger_components": {}})
    monkeypatch.setattr(ui_app, "approve_pending", lambda skill, actor="?": f"promoted '{skill}'")
    r = client.post("/api/promote/pdf")
    assert r.status_code == 200 and r.json() == {"result": "promoted 'pdf'"}


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
    import optimize
    def conflict():
        raise SystemExit("error: provider pin conflicts detected before spending any tokens:\n  MODEL=x: nope")
    monkeypatch.setattr(optimize, "preflight_provider_pins", conflict)
    r = client.post("/api/optimize/pdf")
    assert r.status_code == 400 and "pin conflicts" in r.json()["detail"]


def test_skills_api_reports_eval_status_for_all_skills(client, tmp_path, monkeypatch):
    # the UI's evals chip keys off has_tasks, every skill must carry it, task set or not
    from mcp_server import registry
    from mcp_server.registry import write_skill_md
    import ui.app as U
    for name in ("with-evals", "without-evals"):
        d = registry.SKILLS_DIR / name   # hermetic per-test root (conftest)
        d.mkdir(parents=True)
        write_skill_md(d / "SKILL.md", {"name": name, "description": "d"}, "b")
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "with-evals.yaml").write_text("train: []")
    monkeypatch.setattr(U, "TASKS_DIR", tasks)
    flags = {s["name"]: s["has_tasks"] for s in client.get("/api/skills").json()}
    assert flags == {"with-evals": True, "without-evals": False}


def test_index_ships_eval_chips_and_disabled_candidate_run(client):
    html = client.get("/").text
    assert "no evals" in html          # chip for skills without an eval task set
    assert "has_tasks" in html         # rendering keys off the API flag
    assert "auto-drafts" in html       # the disabled generate button explains how to get evals
    assert "Optimize with SkillOpt" in html  # optimization is a first-class, human-gated workflow


def test_index_leads_with_review_before_candidate_generation(client):
    """Reviewers must meet the evidence and the decision first; generation is downstream."""
    html = client.get("/").text
    assert html.index('id="review-section"') < html.index('id="history-section"')
    assert html.index('id="history-section"') < html.index('id="skills"')
    assert html.index('id="skills"') < html.index('id="run-section"')
    assert "Evidence-gated change control" in html
    assert "change control" in html and "skill optimizer" not in html


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
    from mcp_server.registry import optimizable_components, skill_revision
    root = tmp_path / "skills"
    skill = root / "pdf"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: pdf\ndescription: Merge PDFs.\n---\napproved body\n")
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(root))
    champion = optimizable_components(skill)
    challenger = {**champion, "body": "promoted body"}
    from mcp_server.registry import load_skills
    current = load_skills(root)[0]
    gate = {"promotable": True, "blocked": []}
    P.save_pending("pdf", {
        "skill": "pdf", "gate": gate,
        "champion_components": champion, "challenger_components": challenger,
        "evidence": {"champion": {"revision": current.revision},
                     "challenger": {"revision": skill_revision(skill, challenger)},
                     "gate": gate},
    })
    P.approve_pending("pdf")
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
    from mcp_server import registry
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


def test_rollback_restores_a_snapshot_and_records_it(client, tmp_path, monkeypatch):
    skill, replaced = _promoted_skill(tmp_path, monkeypatch)
    assert "promoted body" in (skill / "SKILL.md").read_text()

    r = client.post(f"/api/rollback/pdf/{replaced}")

    assert r.status_code == 200 and "Rolled back" in r.json()["result"]
    assert "approved body" in (skill / "SKILL.md").read_text()
    trail = client.get("/api/history").json()["audit"]["records"]
    assert [a["action"] for a in trail] == ["rollback", "approve"]


def test_rollback_rejects_unknown_revision_and_bad_names(client, tmp_path, monkeypatch):
    _promoted_skill(tmp_path, monkeypatch)
    assert client.post("/api/rollback/pdf/deadbeef").status_code == 404
    assert client.post("/api/rollback/Not_A_Slug/deadbeef").status_code == 400


def test_rollback_refuses_a_traversing_revision_at_the_application(client, tmp_path, monkeypatch):
    """A `..` segment must be refused by revision validation, not merely missed by the router:
    the same string reaching optimize.promote directly has to be rejected there too."""
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
    from mcp_server.registry import load_skills, optimizable_components, skill_revision
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
    from mcp_server.registry import load_skills, optimizable_components, skill_revision
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
    monkeypatch.setattr(U, "REPO_ROOT", tmp_path.resolve())
    monkeypatch.setattr(U, "EVIDENCE_DIR", evidence_root)
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
    assert "showPending(quarantined[0], {keepMessage: true});" in html
    assert "if (!quarantined.length) { showNoPending(); return; }" in html
    # a card opened by hand still clears the previous result
    assert "if (!keepMessage) say(\"#pending-msg\", \"\", false);" in html
    assert 'onclick="showPending(\'${esc(s.name)}\')"' in html


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
    from mcp_server.registry import load_skills, optimizable_components, skill_revision
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
    P.approve_pending("pdf")

    listed = [r["revision"] for r in client.get("/api/history").json()["revisions"]["pdf"]]
    assert listed == [second, first]
