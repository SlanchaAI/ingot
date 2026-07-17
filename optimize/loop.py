"""Continuous optimization loop: mine each skill's real traces for health, then optimize the ones
that are actually failing — stitching mining → (auto-drafted) eval set → GEPA A/B → *gated* pending
into one command. Nothing auto-promotes; every surviving improvement lands in the approval UI.

Usage: python -m optimize.loop [skill ...]   (default: every skill with an eval task set)
"""
import argparse
import os

from .ab import TASKS_DIR, run_ab
from .mine import mine

HEALTH_THRESHOLD = float(os.environ.get("LOOP_HEALTH_THRESHOLD", "0.7"))  # mine mean below this = optimize
# Which passes an unhealthy skill gets, in order. "body" = quality GEPA + full-agent A/B;
# "description" = the routing-objective pass (embedding-scored, ~free; routing cases auto-draft).
PASSES = [p.strip() for p in os.environ.get("LOOP_PASSES", "body").split(",") if p.strip()]
_KNOWN_PASSES = ("body", "description")


def skills_with_tasksets() -> list[str]:
    return sorted(p.stem for p in TASKS_DIR.glob("*.yaml"))


def loop(skills: list[str] | None = None, force: bool = False, budget: int = 60, log=print) -> dict:
    targets = skills or skills_with_tasksets()
    if not targets:
        log("[loop] no skills have eval task sets yet — nothing to optimize.")
        return {}
    results = {}
    for skill in targets:
        log(f"\n[loop] ===== {skill} =====")
        try:
            health = mine(skill, log=log)
            mean = health["mean_score"]
        except SystemExit as e:              # no traces yet — optimize anyway if forced, else skip
            log(f"[loop] {skill}: no trace signal ({e})")
            health, mean = None, None
        if not force and mean is not None and mean >= HEALTH_THRESHOLD:
            log(f"[loop] {skill}: healthy (mean {mean:.2f} ≥ {HEALTH_THRESHOLD}) — skipping optimize.")
            results[skill] = {"optimized": False, "mean_score": mean}
            continue
        log(f"[loop] {skill}: below health bar (mean {mean if mean is not None else 'n/a'}) — optimizing…")
        unknown = [p for p in PASSES if p not in _KNOWN_PASSES]
        if unknown:
            raise SystemExit(f"LOOP_PASSES names unknown pass(es) {unknown}; known: {_KNOWN_PASSES}")
        passes = {}
        for pass_name in PASSES:
            if pass_name == "body":
                r = run_ab(skill, budget=budget, log=log)
            else:
                from .routing import run_routing
                r = run_routing(skill, log=log)
            passes[pass_name] = {"improved": r.get("improved"), "gate": r.get("gate")}
        gate = next((p["gate"] for p in passes.values() if p.get("gate", {}) and p["gate"].get("promotable")),
                    passes.get("body", {}).get("gate"))
        results[skill] = {"optimized": True, "passes": passes, "gate": gate, "mean_score": mean,
                          "improved": any(p.get("improved") for p in passes.values())}
    queued = [s for s, r in results.items() if r.get("optimized") and (r.get("gate") or {}).get("promotable")]
    log(f"\n[loop] done. {len(queued)} challenger(s) passed the gate and are queued for review "
        f"at http://localhost:8080: {queued}")
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("skills", nargs="*", help="skills to check (default: all with an eval task set)")
    ap.add_argument("--force", action="store_true", help="optimize even skills that look healthy")
    ap.add_argument("--budget", type=int, default=60, help="GEPA max metric calls per skill")
    args = ap.parse_args()
    from . import require_openrouter_key
    require_openrouter_key()
    loop(args.skills or None, force=args.force, budget=args.budget)
