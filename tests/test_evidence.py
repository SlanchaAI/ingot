import json

from optimize.evidence import build_evidence, first_divergence, render_markdown, write_evidence


SUMMARY = {
    "skill": "pdf",
    "created": 123,
    "dataset": "pdf-holdout",
    "harness": "codex",
    "model": "test-model",
    "split": {"kind": "holdout", "leakage": False},
    "changed_components": ["body"],
    "gate": {"promotable": True, "blocked": []},
    "ab": {
        "champion": {"mean": 0.25, "scores": [0.0, 0.5],
                     "tokens": {"mean_input": 100, "mean_output": 50}},
        "challenger": {"mean": 0.75, "scores": [0.5, 1.0],
                       "tokens": {"mean_input": 90, "mean_output": 40}},
    },
    "behavior": {
        "champion": [[{"type": "tool", "name": "route_and_load"}, {"type": "final", "sha256": "a"}],
                     [{"type": "tool", "name": "route_and_load"}]],
        "challenger": [[{"type": "tool", "name": "route_and_load"}, {"type": "final", "sha256": "b"}],
                       [{"type": "tool", "name": "route_and_load"}]],
    },
}


def test_first_divergence_finds_behavioral_fork():
    left = [{"name": "route"}, {"name": "bash", "args": {"x": 1}}]
    right = [{"name": "route"}, {"name": "write", "args": {"x": 1}}]
    assert first_divergence(left, right) == {"index": 1, "champion": left[1], "challenger": right[1]}
    assert first_divergence(left, list(left)) is None


def test_build_evidence_attributes_revisions_and_per_case_deltas():
    evidence = build_evidence(SUMMARY, "champ-rev", "chall-rev")
    assert evidence["schema_version"] == "skill-router/evidence/v1"
    assert evidence["champion"]["revision"] == "champ-rev"
    assert evidence["challenger"]["revision"] == "chall-rev"
    assert evidence["cases"][0]["score_delta"] == 0.5
    assert evidence["cases"][0]["first_divergence"]["index"] == 1
    assert evidence["gate"]["promotable"] is True


def test_write_evidence_emits_json_and_deterministic_report(tmp_path):
    evidence = build_evidence(SUMMARY, "champ-rev", "chall-rev")
    json_path, md_path = write_evidence(evidence, tmp_path)
    assert json.loads(json_path.read_text()) == evidence
    assert md_path.read_text() == render_markdown(evidence)
    text = md_path.read_text()
    assert md_path.name == "EVIDENCE.md"
    assert "Behavioral Skill CI" in text and "0.250 → 0.750" in text
    assert "PASS" in text and "champ-rev" in text


def test_evidence_marks_leaky_split_and_structural_divergence():
    summary = {**SUMMARY, "split": {"kind": "none", "leakage": True}}
    evidence = build_evidence(summary, "champ", "chall")
    assert evidence["split"]["leakage"] is True
    assert "structural" in render_markdown(evidence).lower()


def test_markdown_surfaces_gate_warnings():
    summary = {**SUMMARY, "gate": {"promotable": True, "blocked": [],
                                   "warnings": ["challenger drops 90% of the champion body"]}}
    md = render_markdown(build_evidence(summary, "c1", "c2"))
    assert "- Warnings: challenger drops 90% of the champion body" in md
    md_none = render_markdown(build_evidence(SUMMARY, "c1", "c2"))
    assert "- Warnings: none" in md_none


# --- routing pass -----------------------------------------------------------------------------

ROUTING_METRICS = {
    "champion": {"top1": 0.500, "recall_at_3": 0.500, "no_route_precision": 0.000},
    "challenger": {"top1": 1.000, "recall_at_3": 1.000, "no_route_precision": 0.333},
    "parity": {"rate": 1.0, "total": 4},
}


def _routing_evidence(gate=None, parity=True):
    from optimize.evidence import RoutingRun, build_routing_evidence
    metrics = dict(ROUTING_METRICS)
    if not parity:
        metrics["parity"] = {"rate": 0.0, "total": 0}
    return build_routing_evidence(RoutingRun(
        skill="pdf", created=123, dataset="pdf-routing", metrics=metrics,
        champion_revision="champ-rev", challenger_revision="chall-rev",
        inner_loop={"seed_score": 0.286, "best_score": 0.714, "budget": 60},
        gate=gate or {"promotable": True, "blocked": [], "warnings": []}))


def test_routing_evidence_carries_revisions_and_router_metrics():
    from optimize.evidence import ROUTING_SCHEMA
    evidence = _routing_evidence()
    assert evidence["schema_version"] == ROUTING_SCHEMA
    assert evidence["champion"]["revision"] == "champ-rev"
    assert evidence["challenger"]["revision"] == "chall-rev"
    assert evidence["challenger"]["routing"]["top1"] == 1.0
    assert evidence["changed_components"] == ["description"]


def test_write_evidence_renders_the_routing_report(tmp_path):
    from optimize.evidence import render_routing_markdown
    evidence = _routing_evidence()
    json_path, md_path = write_evidence(evidence, tmp_path)
    assert json.loads(json_path.read_text()) == evidence
    text = md_path.read_text()
    assert text == render_routing_markdown(evidence)
    assert "Routing evidence: pdf" in text
    assert "champ-rev" in text and "chall-rev" in text
    assert "| top1 | 0.500 | 1.000 | +0.500 |" in text
    assert "Cross-harness parity: 1.000" in text
    assert "PASS" in text


def test_routing_report_marks_a_blocked_gate_and_unexercised_parity():
    from optimize.evidence import render_routing_markdown
    blocked = {"promotable": False, "blocked": ["routing top1 regressed"], "warnings": []}
    text = render_routing_markdown(_routing_evidence(gate=blocked, parity=False))
    assert "BLOCKED" in text
    assert "- Blocking reasons: routing top1 regressed" in text
    assert "Cross-harness parity: not exercised" in text


def test_recorded_path_is_repo_relative_and_leaves_outside_paths_alone(tmp_path):
    from pathlib import Path

    from optimize.evidence import _REPO_ROOT, recorded_path
    inside = _REPO_ROOT / "runs" / "evidence" / "pdf" / "1" / "EVIDENCE.md"
    assert recorded_path(inside) == "runs/evidence/pdf/1/EVIDENCE.md"
    outside = Path("/somewhere/else/EVIDENCE.md")
    assert recorded_path(outside) == "/somewhere/else/EVIDENCE.md"
