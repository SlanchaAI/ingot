"""Held-out routing evaluation for descriptions, filters, and no-route behavior."""
from __future__ import annotations

from pathlib import Path

import yaml


def load_cases(path: Path) -> list[dict]:
    paths = sorted([*path.glob("*.yaml"), *path.glob("*.yml")]) if path.is_dir() else [path]
    cases = []
    for source in paths:
        data = yaml.safe_load(source.read_text()) or {}
        for case in data.get("cases", []):
            cases.append(dict(case))
    return cases


def evaluate_cases(router, cases: list[dict], *, min_score: float = 0.65) -> dict:
    positives = top1_hits = recall_hits = 0
    no_route = no_route_hits = 0
    failures = []
    for index, case in enumerate(cases):
        expected = case.get("expected")
        context = {
            "harness": case.get("harness", "codex"),
            "cwd": case.get("cwd", "."),
            "available_tools": case.get("available_tools", []),
            "available_mcps": case.get("available_mcps", []),
            "platform": case.get("platform"),
            "min_score": case.get("min_score", min_score),
        }
        result = router.route(case["task"], **context)
        ranked = [result.get("match")] + [item["name"] for item in result.get("alternatives", [])]
        if expected is None:
            no_route += 1
            passed = result.get("match") is None
            no_route_hits += int(passed)
        else:
            positives += 1
            top1 = result.get("match") == expected
            recalled = expected in ranked[:3]
            top1_hits += int(top1)
            recall_hits += int(recalled)
            passed = top1
        if not passed:
            failures.append({"index": index, "task": case["task"], "expected": expected,
                             "actual": result.get("match"), "alternatives": ranked[1:3]})
    return {
        "total": len(cases),
        "top1": round(top1_hits / positives, 6) if positives else 1.0,
        "recall_at_3": round(recall_hits / positives, 6) if positives else 1.0,
        "no_route_precision": round(no_route_hits / no_route, 6) if no_route else 1.0,
        "failures": failures,
    }


def evaluate_parity(router, cases: list[dict], *, min_score: float = 0.65) -> dict:
    selected = [case for case in cases if case.get("parity")]
    failures = []
    for index, case in enumerate(selected):
        context = {
            "cwd": case.get("cwd", "."),
            "available_tools": case.get("available_tools", []),
            "available_mcps": case.get("available_mcps", []),
            "platform": case.get("platform"),
            "min_score": case.get("min_score", min_score),
        }
        claude = router.route(case["task"], harness="claude", **context)
        codex = router.route(case["task"], harness="codex", **context)
        if (claude.get("match"), claude.get("revision")) != (codex.get("match"), codex.get("revision")):
            failures.append({"index": index, "task": case["task"],
                             "claude": {"match": claude.get("match"), "revision": claude.get("revision")},
                             "codex": {"match": codex.get("match"), "revision": codex.get("revision")}})
    total = len(selected)
    return {"total": total, "rate": round((total - len(failures)) / total, 6) if total else 1.0,
            "failures": failures}
