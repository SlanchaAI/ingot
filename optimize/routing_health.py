"""Library-wide routing health check, embedding-only (no LLM, no key, no cost).

Routing quality decays as the library grows: a newly added skill's description can shadow an
older one for traffic neither suite ever re-tested. This check catches that drift by replaying
every skill's `routing:` suite against the REAL current router (the full library, not a variant),
plus a pairwise description-collision scan at the same COLLISION_SCORE cutoff the promotion gate
uses. Read-only; it proposes nothing and never touches the review queue.

Usage: python -m optimize.routing_health [skill ...]   (default: every skill with routing cases)
Exit status is non-zero when any suite case fails or any collision is found, so it can run
unattended from cron or CI: docker compose run --rm optimize python -m optimize.routing_health
"""
import argparse
from pathlib import Path

import yaml

from mcp_server.registry import load_skills
from mcp_server.router import Router
from mcp_server.routing_eval import evaluate_cases, evaluate_parity

from .ab import COLLISION_SCORE, TASKS_DIR


def load_routing_cases(skill: str, tasks_dir: Path | None = None) -> list[dict]:
    path = (tasks_dir or TASKS_DIR) / f"{skill}.yaml"
    if not path.exists():
        return []
    return (yaml.safe_load(path.read_text()) or {}).get("routing") or []


def check_suites(router: Router, skills: list[str], log=print) -> list[str]:
    """Replay each skill's routing suite against the live router; one problem line per failure."""
    problems = []
    for skill in skills:
        cases = load_routing_cases(skill)
        if not cases:
            log(f"[health] {skill}: no routing cases, skipped")
            continue
        metrics = evaluate_cases(router, cases)
        parity = evaluate_parity(router, cases)
        log(f"[health] {skill}: top1 {metrics['top1']:.3f} · recall@3 {metrics['recall_at_3']:.3f}"
            f" · no-route precision {metrics['no_route_precision']:.3f} ({metrics['total']} cases)")
        for f in metrics["failures"]:
            if f["expected"] is None and f["actual"] != skill:
                # Another skill claimed the task, which may be exactly right (the library owns
                # tasks a single suite's negatives can't know about). At the library level a
                # suite's no-route cases can only hold ITS skill to not over-triggering; the
                # promotion-time routing pass stays strict about match-nothing.
                continue
            problems.append(f"{skill}: case '{f['task']}' expected "
                            f"{f['expected'] or 'no route'} but routed to {f['actual']}")
        if parity["total"] and parity["rate"] < 1.0:
            problems.append(f"{skill}: cross-harness parity {parity['rate']:.3f} < 1.000")
    return problems


def check_collisions(log=print) -> list[str]:
    """Pairwise description-collision scan over the active library, each reported once."""
    skills = load_skills()
    problems, seen = [], set()
    for skill in skills:
        others = [s for s in skills if s.name != skill.name]
        if not others:
            continue
        shadowed, score = Router(others).nearest(skill.description)
        pair = frozenset((skill.name, shadowed))
        if score >= COLLISION_SCORE and pair not in seen:
            seen.add(pair)
            problems.append(f"'{skill.name}' and '{shadowed}' have colliding descriptions "
                            f"(cosine {score:.2f} ≥ {COLLISION_SCORE}), one shadows the other")
    return problems


def run_health(skills: list[str] | None = None, log=print) -> list[str]:
    """All problems found (empty = healthy). Suites replay against the full current library."""
    targets = skills or sorted(p.stem for p in TASKS_DIR.glob("*.yaml"))
    router = Router(load_skills())
    problems = check_suites(router, targets, log=log) + check_collisions(log=log)
    if problems:
        log(f"\n[health] ⛔ {len(problems)} problem(s):")
        for p in problems:
            log(f"[health] - {p}")
        log("[health] fix: refine the colliding/regressed description by hand, or run the "
            "routing pass (python -m optimize.ab <skill> --description) and review the result.")
    else:
        log("\n[health] ✓ routing healthy: every suite passes and no descriptions collide.")
    return problems


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="python -m optimize.routing_health")
    ap.add_argument("skills", nargs="*",
                    help="skills to check (default: every skill with an eval task set)")
    raise SystemExit(1 if run_health(ap.parse_args().skills or None) else 0)
