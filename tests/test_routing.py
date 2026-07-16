"""Routing pass (optimize --description): adapter scoring against a scripted router, and the
no-regression/improvement/collision gate. No embeddings, no LLM — the router is injected."""
import pytest

from optimize import routing as R


class _ScriptedRouter:
    """route() answers from a task -> (match, alternatives) script."""

    def __init__(self, script):
        self._script = script

    def route(self, task, harness, cwd, available_tools=None, available_mcps=None):
        match, alternatives = self._script[task]
        return {"match": match, "alternatives": [{"name": n} for n in alternatives]}


def _evaluate(script, cases):
    adapter = R.RoutingAdapter("pdf", router_factory=lambda desc: _ScriptedRouter(script))
    return adapter.evaluate(cases, {"description": "candidate"}, capture_traces=True)


def test_adapter_scores_expected_top3_miss_and_no_route():
    cases = [
        {"task": "merge pdfs", "expected": "pdf"},        # exact -> 1.0
        {"task": "fill a form", "expected": "pdf"},       # top-3 near miss -> 0.5
        {"task": "rotate pages", "expected": "pdf"},      # not in top 3 -> 0.0
        {"task": "hello", "expected": None},              # correctly no-route -> 1.0
        {"task": "thanks", "expected": None},             # spurious match -> 0.0
    ]
    script = {
        "merge pdfs": ("pdf", []),
        "fill a form": ("docx", ["pdf"]),
        "rotate pages": ("docx", ["pptx", "xlsx"]),
        "hello": (None, []),
        "thanks": ("pdf", []),
    }
    batch = _evaluate(script, cases)
    assert batch.scores == [1.0, 0.5, 0.0, 1.0, 0.0]
    feedback = [t["feedback"] for t in batch.trajectories]
    assert "as expected" in feedback[0]
    assert "ranked behind 'docx'" in feedback[1]
    assert "not in the top 3" in feedback[2]
    assert "correctly matched no skill" in feedback[3]
    assert "triggers too broadly" in feedback[4]


def test_adapter_reflective_dataset_carries_routing_diagnosis():
    cases = [{"task": "merge pdfs", "expected": "pdf"}]
    batch = _evaluate({"merge pdfs": ("docx", [])}, cases)
    adapter = R.RoutingAdapter("pdf", router_factory=lambda d: None)
    records = adapter.make_reflective_dataset({"description": "d"}, batch, ["description"])
    assert "routing trigger" in records["description"][0]["Diagnosis"]
    assert records["description"][0]["Inputs"] == "merge pdfs"


def _metrics(champ, chall, parity=None):
    return {"champion": champ, "challenger": chall,
            "parity": parity or {"rate": 1.0, "total": 2}}


_PERFECT = {"top1": 1.0, "recall_at_3": 1.0, "no_route_precision": 1.0}
_WEAK = {"top1": 0.5, "recall_at_3": 0.5, "no_route_precision": 1.0}


def test_gate_passes_a_strict_improvement(monkeypatch):
    monkeypatch.setattr(R, "_description_shadows", lambda s, d: ("", 0.0))
    ok, reasons = R.routing_gate("pdf", _metrics(_WEAK, _PERFECT), {"description": "d"})
    assert ok and reasons == []


def test_gate_blocks_any_regression(monkeypatch):
    monkeypatch.setattr(R, "_description_shadows", lambda s, d: ("", 0.0))
    ok, reasons = R.routing_gate("pdf", _metrics(_PERFECT, _WEAK), {"description": "d"})
    assert not ok and any("regressed" in r for r in reasons)


def test_gate_blocks_no_improvement(monkeypatch):
    monkeypatch.setattr(R, "_description_shadows", lambda s, d: ("", 0.0))
    ok, reasons = R.routing_gate("pdf", _metrics(_PERFECT, dict(_PERFECT)), {"description": "d"})
    assert not ok and any("no routing metric improved" in r for r in reasons)


def test_gate_blocks_collision_and_parity(monkeypatch):
    monkeypatch.setattr(R, "_description_shadows", lambda s, d: ("docx", 0.97))
    ok, reasons = R.routing_gate("pdf", _metrics(_WEAK, _PERFECT, parity={"rate": 0.5, "total": 2}),
                                 {"description": "d"})
    assert not ok
    assert any("shadows 'docx'" in r for r in reasons)
    assert any("parity" in r for r in reasons)
