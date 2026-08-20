"""Score one skill as it stands and report which checks it fails, without changing anything.

The optimize loop answers "is this candidate better than the champion". That is the wrong question
when you have just written a skill and want to know whether it is any good, and it is unreachable
anyway until an eval set exists. This runs the skill against its own tasks, grades every answer
against that task's checklist, and ranks the failures by how much they cost -- so the output is a
list of things to fix, not a number.

It never writes a pending record, never promotes, and never touches the skill directory. Drafting an
eval set is the one side effect, and only when the skill has none (see optimize/draft.py).

    python -m ingot.optimize.review <skill>
"""
import argparse
import json
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ingot.mcp_server.registry import optimizable_components, skill_revision

from . import SERVE_TEMPLATE, agent_model, resolve_skill_dir
from . import usage as usage_ledger
from .ab import load_tasks
from .compat import _llm
from .judge import DIMENSIONS, invoke_retry, judge
from .rollout import assemble
from ingot import paths

REVIEW_DIR = paths.runs() / "reviews"
_MAX_WORKERS = 8


def _review_one(llm, system: str, task: dict) -> dict:
    """Answer one task with the skill loaded, then grade that answer against the task's checklist."""
    msg = invoke_retry(llm, [("system", system), ("user", task["task"])])
    usage_ledger.add("review", getattr(msg, "usage_metadata", None))
    verdict = judge(task["task"], task.get("rubric", ""), msg.content,
                    check=task.get("check"), deliverable=task.get("deliverable"),
                    checklist=task.get("checklist"))
    return {"task": task["task"], "score": verdict["score"], "answer": msg.content,
            "feedback": verdict["feedback"], "checklist": verdict["checklist"],
            "spec": {i["id"]: i for i in (task.get("checklist") or [])}}


def findings(results: list[dict]) -> list[dict]:
    """Every check that did not clean-pass, worst first.

    Ranked by weight x shortfall rather than by score: a weight-5 check scraping a partial matters
    more than a weight-1 check failing outright, and sorting by the raw value buries it."""
    out = []
    for i, result in enumerate(results):
        for check_id, graded in result["checklist"].items():
            if graded["value"] >= 1.0:
                continue
            spec = result["spec"].get(check_id, {})
            weight = float(spec.get("weight", 1))
            out.append({"task_index": i, "task": result["task"], "check": check_id,
                        "criterion": spec.get("criterion", ""), "weight": weight,
                        "dimension": spec.get("dimension", ""), "value": graded["value"],
                        "note": graded["note"], "cost": weight * (1.0 - graded["value"])})
    return sorted(out, key=lambda f: -f["cost"])


def by_dimension(found: list[dict]) -> dict:
    """Where the losses concentrate. A skill failing everything on completeness needs a different
    edit from one failing scattered correctness checks."""
    totals = {d: 0.0 for d in DIMENSIONS}
    for f in found:
        if f["dimension"] in totals:
            totals[f["dimension"]] += f["cost"]
    return {d: round(v, 3) for d, v in sorted(totals.items(), key=lambda kv: -kv[1]) if v}


def run_review(skill: str, log=print) -> dict:
    usage_ledger.reset()
    skill_dir = resolve_skill_dir(skill)
    # A review lasts about a minute. Copy once so a concurrent promotion or trusted filesystem edit
    # cannot make the revision describe one read while the prompt grades a later read.
    with tempfile.TemporaryDirectory(prefix=f"ingot-review-{skill}-") as temporary:
        snapshot = Path(temporary) / skill_dir.name
        shutil.copytree(skill_dir, snapshot, symlinks=True)
        revision = skill_revision(snapshot)
        components = optimizable_components(snapshot)
        train, holdout, _ = load_tasks(
            skill, draft_components=components)    # draft against the same revision being graded
    tasks = list(train) + list(holdout)            # nothing is being generalized to; grade on all
    if not tasks:
        raise SystemExit(f"'{skill}' has no eval tasks to run.")
    model = agent_model()
    system = SERVE_TEMPLATE.format(body=assemble(components))
    graded = sum(len(t.get("checklist") or []) for t in tasks)
    log(f"[review] '{skill}': {len(tasks)} tasks, {graded or len(tasks) * 4} checks, on {model}")

    llm = _llm(model)
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(tasks))) as pool:
        results = list(pool.map(lambda t: _review_one(llm, system, t), tasks))

    found = findings(results)
    score = sum(r["score"] for r in results) / len(results)
    summary = {
        "skill": skill, "model": model, "revision": revision,
        "score": round(score, 4), "tasks": len(tasks),
        "checks": sum(len(r["checklist"]) for r in results),
        "failed_checks": len(found),
        "by_dimension": by_dimension(found),
        "findings": [{k: v for k, v in f.items() if k != "task_index"} for f in found],
        "per_task": [{"task": r["task"], "score": round(r["score"], 4)} for r in results],
    }
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEW_DIR / f"{skill}.json"
    path.write_text(json.dumps(summary, indent=2))

    log(f"\n[review] {skill}: {score:.3f} over {len(tasks)} tasks "
        f"({summary['checks'] - len(found)}/{summary['checks']} checks clean)")
    if summary["by_dimension"]:
        log("[review] losses by dimension: " +
            ", ".join(f"{d} {v}" for d, v in summary["by_dimension"].items()))
    for f in found[:10]:
        log(f"  - {f['check']} (weight {f['weight']:g}, {'partial' if f['value'] else 'fail'})"
            f" — {f['note'] or f['criterion']}")
    if len(found) > 10:
        log(f"  … {len(found) - 10} more in {path}")
    log(f"[review] wrote {path}")
    log(usage_ledger.format_report())
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Score a skill against its own eval tasks and report "
                                             "which checks it fails. Changes nothing.")
    ap.add_argument("skill")
    args = ap.parse_args()
    if os.environ.get("REVIEW_REQUIRE_KEY", "1") != "0":
        from . import require_openrouter_key
        require_openrouter_key()
    run_review(args.skill)
