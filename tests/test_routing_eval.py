from mcp_server.routing_eval import evaluate_cases, evaluate_parity, load_cases
from mcp_server.registry import load_skills
from mcp_server.router import Router
from pathlib import Path


class FakeRouter:
    def route(self, task, **context):
        fixtures = {
            "pdf": {"match": "pdf", "alternatives": [{"name": "docs"}]},
            "alt": {"match": "docs", "alternatives": [{"name": "pdf"}]},
            "none": {"match": None, "alternatives": []},
            "false-positive": {"match": "pdf", "alternatives": []},
        }
        return fixtures[task]


def test_evaluate_cases_reports_selection_and_no_route_metrics():
    cases = [
        {"task": "pdf", "expected": "pdf"},
        {"task": "alt", "expected": "pdf"},
        {"task": "none", "expected": None},
        {"task": "false-positive", "expected": None},
    ]
    result = evaluate_cases(FakeRouter(), cases)
    assert result["total"] == 4
    assert result["top1"] == 0.5
    assert result["recall_at_3"] == 1.0
    assert result["no_route_precision"] == 0.5
    assert len(result["failures"]) == 2


def test_load_cases_reads_all_yaml_files_in_directory(tmp_path):
    (tmp_path / "a.yaml").write_text("cases:\n  - task: one\n    expected: pdf\n")
    (tmp_path / "b.yml").write_text("cases:\n  - task: two\n    expected: null\n")
    assert [case["task"] for case in load_cases(tmp_path)] == ["one", "two"]


def test_evaluate_parity_compares_claude_and_codex_routes():
    class ParityRouter:
        def route(self, task, harness, **context):
            match = "same" if task == "same" else harness
            return {"match": match, "revision": "rev", "alternatives": []}

    result = evaluate_parity(ParityRouter(), [
        {"task": "same", "parity": True},
        {"task": "different", "parity": True},
        {"task": "ignored"},
    ])
    assert result["total"] == 2 and result["rate"] == 0.5
    assert result["failures"][0]["task"] == "different"


def test_committed_suite_covers_filter_and_parity_contract():
    root = Path(__file__).resolve().parent.parent
    cases = load_cases(root / "evals" / "routing.yaml")
    result = evaluate_cases(Router(load_skills(root / "evals" / "fixtures" / "skills")), cases)
    parity = evaluate_parity(Router(load_skills(root / "evals" / "fixtures" / "skills")), cases)
    assert len(cases) >= 10
    assert result["failures"] == []
    assert result["recall_at_3"] == 1.0 and result["no_route_precision"] == 1.0
    assert parity["rate"] == 1.0 and parity["total"] >= 2
