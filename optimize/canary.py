"""Canary promotion — the production-honest alternative to the offline A/B.

Instead of scoring a fixed eval set, serve the challenger to a small fraction (epsilon) of live
traffic, judge each *real* outcome, and promote only once the challenger's posterior beats the
champion's under a sequential Thompson-sampling decision. This protects live traffic (most requests
still get the champion) and gates on outcomes the optimizer never trained on.

Mechanism (faithful to production): per request, an epsilon coin routes to champion or challenger;
the outcome is judged; each variant keeps a Beta(successes+1, failures+1) posterior; after each
request we estimate P(challenger > champion) by sampling both posteriors and stop early to promote
(P >= promote_p) or reject (P <= 1-promote_p) once both arms have min_samples.

Traffic source: here the skill's own task set is cycled as a stand-in for live requests, and the
LLM judge is the outcome signal. In production the stream is real user requests and the outcome is
your real signal (thumbs, task success, or a reference-free judge) — swap `_next_request` / the
scorer and the decision logic is unchanged.

Usage: python -m optimize.canary <skill> [--epsilon 0.25] [--max 24] [--challenger-file F]
"""
import argparse
import asyncio
import json
import random
import time
from pathlib import Path

import numpy as np

from agent.run import build_agent, langfuse_config, run_task
from mcp_server.registry import SKILLS_DIR, optimizable_components, skill_revision

from . import usage as usage_ledger
from .ab import EVAL_INSTRUCTIONS, _variant_tools, load_tasks
from .judge import judge
from .promote import load_pending, promote, save_pending


def p_challenger_better(champ: dict, chall: dict, draws: int = 4000) -> float:
    """P(challenger success-rate > champion success-rate) from their Beta posteriors."""
    a = np.random.beta(champ["a"], champ["b"], draws)
    b = np.random.beta(chall["a"], chall["b"], draws)
    return float(np.mean(b > a))


def record_outcome(lf, trace_id: str, verdict: dict, success: bool) -> None:
    """Write the judged canary outcome back onto the request's Langfuse trace, so arm state is
    auditable and the posterior can be recomputed from stored scores at any time."""
    if not (lf and trace_id):
        return
    lf.create_score(trace_id=trace_id, name="canary_judge", value=verdict["score"],
                    comment=(verdict.get("feedback") or "")[:500])
    lf.create_score(trace_id=trace_id, name="canary_success", value=float(success))


def run_canary(skill: str, challenger_file: str | None = None, epsilon: float = 0.25,
               min_samples: int = 5, max_requests: int = 24, promote_p: float = 0.95,
               success_thresh: float = 0.5, auto_promote: bool = False, seed: int = 0, log=print) -> dict:
    usage_ledger.reset()
    rng = random.Random(seed)
    np.random.seed(seed)

    skill_dir = SKILLS_DIR / skill
    if not (skill_dir / "SKILL.md").exists():
        raise SystemExit(f"No skill named '{skill}' in skills/.")
    champion = optimizable_components(skill_dir)
    if challenger_file:
        challenger = json.loads(Path(challenger_file).read_text())["components"]
    else:
        pending = load_pending(skill)
        if not pending:
            raise SystemExit(f"No challenger for '{skill}': run `optimize {skill}` first or pass --challenger-file.")
        challenger = pending["challenger_components"]

    train, holdout, _split = load_tasks(skill)
    stream = train + holdout  # stand-in for live traffic
    agents = {v: build_agent(_variant_tools(skill, c["body"], c["description"]),
                             instructions=EVAL_INSTRUCTIONS)
              for v, c in (("champion", champion), ("challenger", challenger))}

    beta = {"champion": {"a": 1.0, "b": 1.0}, "challenger": {"a": 1.0, "b": 1.0}}
    served = {"champion": 0, "challenger": 0}
    # A/B visibility in Langfuse: every request's trace is tagged with its arm + exact skill
    # revision, and the judged outcome is written back as scores (record_outcome).
    lf = None
    if langfuse_config():
        from langfuse import get_client
        lf = get_client()
    revisions = {"champion": skill_revision(skill_dir, champion),
                 "challenger": skill_revision(skill_dir, challenger)}
    log(f"[canary] '{skill}': routing {epsilon:.0%} of traffic to the challenger, judging each outcome "
        f"(promote at P≥{promote_p:.2f}, min {min_samples}/arm, cap {max_requests} requests)…")

    p = 0.5
    for i in range(max_requests):
        task = stream[i % len(stream)]
        variant = "challenger" if rng.random() < epsilon else "champion"
        trace_id = lf.create_trace_id() if lf else None
        tags = [f"canary={variant}", f"revision={revisions[variant]}", skill]
        answer, _, _ = asyncio.run(run_task(agents[variant], task["task"],
                                            config=langfuse_config(tags=tags, trace_id=trace_id)))
        verdict = judge(task["task"], task["rubric"], answer)
        success = verdict["score"] >= success_thresh
        record_outcome(lf, trace_id, verdict, success)
        beta[variant]["a"] += success
        beta[variant]["b"] += (not success)
        served[variant] += 1
        p = p_challenger_better(beta["champion"], beta["challenger"])
        log(f"[canary] req {i+1:>2}: {variant:<10} {'ok' if success else '·'} | "
            f"served champ {served['champion']} / chall {served['challenger']} | P(chall>champ)={p:.2f}")

        if served["challenger"] >= min_samples and served["champion"] >= min_samples:
            if (p >= promote_p or p <= 1 - promote_p) and lf:
                lf.flush()  # decision reached — make sure the queued outcome scores ship
            if p >= promote_p:
                log(f"\n[canary] CHALLENGER WINS live (P={p:.2f} ≥ {promote_p}) after "
                    f"{served['challenger']} canary samples.")
                if auto_promote:
                    log("[canary] " + promote(skill, challenger))
                else:
                    save_pending(skill, {**(load_pending(skill) or {"skill": skill}),
                                         "canary": {"p": p, "served": served, "decision": "promote"}})
                    log("[canary] recorded promote recommendation — approve in the UI (http://localhost:8080).")
                return {"skill": skill, "decision": "promote", "p": p, "served": served}
            if p <= 1 - promote_p:
                log(f"\n[canary] challenger loses live (P={p:.2f} ≤ {1 - promote_p:.2f}) — keeping champion.")
                return {"skill": skill, "decision": "reject", "p": p, "served": served}

    if lf:
        lf.flush()
    log(f"\n[canary] inconclusive after {max_requests} requests (P={p:.2f}) — keeping champion; "
        f"raise --max or --epsilon to gather more challenger samples.")
    return {"skill": skill, "decision": "inconclusive", "p": p, "served": served}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("skill")
    ap.add_argument("--epsilon", type=float, default=0.25, help="fraction of traffic to the challenger")
    ap.add_argument("--max", type=int, default=24, help="max canary requests before giving up")
    ap.add_argument("--min-samples", type=int, default=5, help="min samples per arm before deciding")
    ap.add_argument("--challenger-file", help="use a checkpointed GEPA result instead of the pending challenger")
    ap.add_argument("--promote", action="store_true", help="auto-promote on a live win (else recommend in the UI)")
    args = ap.parse_args()
    from . import require_openrouter_key
    require_openrouter_key()
    run_canary(args.skill, challenger_file=args.challenger_file, epsilon=args.epsilon,
               max_requests=args.max, min_samples=args.min_samples, auto_promote=args.promote)
