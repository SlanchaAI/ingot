import json

from optimize.evidence import build_evidence, component_revision, first_divergence, render_markdown, write_evidence


SUMMARY = {
    "skill": "pdf",
    "created": 123,
    "dataset": "pdf-holdout",
    "harness": "codex",
    "model": "test-model",
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


def test_component_revision_is_stable_and_content_addressed():
    a = component_revision({"description": "d", "body": "b"})
    b = component_revision({"body": "b", "description": "d"})
    assert a == b and len(a) == 64
    assert a != component_revision({"description": "d", "body": "changed"})


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


def test_write_evidence_emits_json_and_deterministic_benchmark(tmp_path):
    evidence = build_evidence(SUMMARY, "champ-rev", "chall-rev")
    json_path, md_path = write_evidence(evidence, tmp_path)
    assert json.loads(json_path.read_text()) == evidence
    assert md_path.read_text() == render_markdown(evidence)
    text = md_path.read_text()
    assert "Behavioral Skill CI" in text and "0.250 → 0.750" in text
    assert "PASS" in text and "champ-rev" in text
