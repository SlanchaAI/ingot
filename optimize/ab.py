"""Optimize a FULL skill and A/B it: GEPA evolves the skill's components (routing description, SKILL.md
body, and any bundled files) into a challenger, then champion vs challenger run through the FULL deep
agent (real MCP suggest_skills, variant get_skill) on the skill's eval task set. Each variant is a
Langfuse dataset run (side-by-side in the UI); the result is written to runs/pending/<skill>.json for
approval in the UI (or promoted directly with --promote).

Usage: python -m optimize.ab <skill> [--promote] [--budget N] [--skip-gepa]
"""
import argparse
import asyncio
import json
import os
import statistics
import time
from dataclasses import replace
from pathlib import Path

import yaml
from langchain_core.tools import tool

from agent.run import _connect, build_agent, langfuse_config, run_task
from mcp_server.registry import SKILLS_DIR, load_skills, optimizable_components

from . import usage as usage_ledger
from .judge import judge
from .promote import promote, save_pending
from .evidence import build_evidence, component_revision, write_evidence

TASKS_DIR = Path(__file__).resolve().parent / "tasks"

# --- promotion gate (anti reward-hacking / overfitting) ---
PROMOTE_MIN_MARGIN = float(os.environ.get("PROMOTE_MIN_MARGIN", "0.15"))   # mean holdout lift required
# ^ set ABOVE the judge's noise floor: identical skill bodies were observed to differ by ~0.10 on
# judge noise alone, so a smaller margin can promote a challenger that isn't actually better.
PROMOTE_MIN_SAMPLES = int(os.environ.get("PROMOTE_MIN_SAMPLES", "3"))       # min held-out tasks
PASS = float(os.environ.get("PROMOTE_PASS_SCORE", "0.5"))                   # a task "passes" at/above this
COLLISION_SCORE = float(os.environ.get("COLLISION_SCORE", "0.93"))          # route-shadow cutoff


def _description_shadows(skill: str, new_description: str) -> tuple[str, float]:
    """Nearest OTHER skill to a GEPA-rewritten description (route-shadow check). ("",0.0) if none."""
    from mcp_server.registry import load_skills
    from mcp_server.router import Router
    others = [s for s in load_skills() if s.name != skill]
    if not others:
        return "", 0.0
    return Router(others).nearest(new_description)


def promotion_gate(skill: str, champ_scores: list[float], chall_scores: list[float],
                   changed: list[str], challenger: dict) -> tuple[bool, list[str]]:
    """A challenger that merely 'wins the mean' can be reward-hacking a small/noisy eval. Require a
    real margin, enough samples, no per-task catastrophic regression, and no routing-shadow from a
    rewritten description. Returns (promotable, reasons-it-was-blocked)."""
    reasons = []
    margin = (statistics.mean(chall_scores) if chall_scores else 0.0) - \
             (statistics.mean(champ_scores) if champ_scores else 0.0)
    if margin < PROMOTE_MIN_MARGIN:
        reasons.append(f"margin {margin:+.2f} < required +{PROMOTE_MIN_MARGIN:.2f}")
    if len(chall_scores) < PROMOTE_MIN_SAMPLES:
        reasons.append(f"only {len(chall_scores)} held-out tasks (< {PROMOTE_MIN_SAMPLES})")
    regressed = [i for i, (c, h) in enumerate(zip(champ_scores, chall_scores)) if c >= PASS and h < PASS]
    if regressed:
        reasons.append(f"catastrophic regression on {len(regressed)} task(s) the champion passed")
    if "description" in changed:
        shadowed, score = _description_shadows(skill, challenger["description"])
        if score >= COLLISION_SCORE:
            reasons.append(f"rewritten description shadows '{shadowed}' (cosine {score:.2f}) — routing hack")
    return (not reasons), reasons

# Eval agents get mutation tools stripped (see _variant_tools), so their instructions must not
# mandate create_skill the way the production INSTRUCTIONS do — an instructed-but-missing tool
# sends the model into tool-not-found retries and corrupts the rollout.
EVAL_INSTRUCTIONS = """You are a deep agent with access to a read-only skill router over MCP.
For every task, call `route_and_load` once with the full task, harness `codex`, current working
directory, and available tools/MCPs. Follow `skill_body` when a match is returned. With no match,
solve directly. Never request a skill catalog. Keep the final answer concise."""


def load_tasks(skill: str, log=print) -> tuple[list[dict], list[dict]]:
    """Return (train, holdout) task lists. GEPA optimizes on train; the A/B + promotion decision is
    judged on holdout — a leakage-clean split so a challenger has to *generalize*, not memorize.
    A task set with only a flat `tasks:` list falls back to train==holdout (no split). If no task set
    exists (e.g. a freshly created skill), the teacher auto-drafts one so the skill is optimizable."""
    p = TASKS_DIR / f"{skill}.yaml"
    if not p.exists():
        from mcp_server.registry import SKILLS_DIR as _SD, read_components
        comps = read_components(_SD / skill)
        from .draft import draft_and_save
        draft_and_save(skill, comps["description"], comps["body"], TASKS_DIR, log=log)
    data = yaml.safe_load(p.read_text())
    train = data.get("train") or data.get("tasks") or []
    holdout = data.get("holdout") or train
    return train, holdout


def _variant_tools(mcp_tools, skill: str, body: str, description: str):
    """One read-only route tool backed by the variant description and body."""
    from mcp_server.router import Router
    skills = load_skills()
    variants = [replace(item, description=description, body=body) if item.name == skill else item
                for item in skills]
    variant_router = Router(variants)

    @tool
    async def route_and_load(task: str, harness: str, cwd: str, available_tools: list[str] = [],
                             available_mcps: list[str] = []) -> dict:
        """Select and load one compatible skill, or return no match."""
        return variant_router.route(task, harness, cwd, available_tools, available_mcps)

    return [route_and_load]


def _run_variant(dataset, variant: str, agent, tasks: list[dict]):
    """One Langfuse dataset run (experiment) for a variant; returns (judge scores, per-task token usage)."""
    from langfuse import Evaluation

    # keyed by task text so scores and usages stay aligned per-task regardless of the order
    # run_experiment processes items in; a failed item defaults to 0
    scores_by_task: dict[str, float] = {}
    usage_by_task: dict[str, dict] = {}
    behavior_by_task: dict[str, list[dict]] = {}

    async def task_fn(*, item, **kwargs):
        # run_experiment drives its own event loop and awaits async tasks
        answer, _, usage, behavior = await run_task(
            agent, item.input["task"], config=langfuse_config(tags=[f"variant={variant}"]),
            include_behavior=True)
        usage_by_task[item.input["task"]] = usage
        behavior_by_task[item.input["task"]] = behavior
        usage_ledger.add("agent_ab", usage)
        return answer

    def judge_evaluator(*, input, output, **kwargs):
        j = judge(input["task"], input["rubric"], str(output))
        scores_by_task[input["task"]] = j["score"]
        return Evaluation(name="judge_score", value=j["score"], comment=j["feedback"])

    def token_evaluator(key):
        def ev(*, input, **kwargs):
            return Evaluation(name=key, value=usage_by_task.get(input["task"], {}).get(key, 0))
        ev.__name__ = key
        return ev

    dataset.run_experiment(name=variant, description=f"A/B variant: {variant}", task=task_fn,
                           evaluators=[judge_evaluator,
                                       token_evaluator("input_tokens"), token_evaluator("output_tokens")])
    return ([scores_by_task.get(t["task"], 0.0) for t in tasks],
            [usage_by_task.get(t["task"], {"input_tokens": 0, "output_tokens": 0}) for t in tasks],
            [behavior_by_task.get(t["task"], []) for t in tasks])


def run_ab(skill: str, promote_now: bool = False, budget: int = 60,
           skip_gepa: bool = False, challenger_file: str | None = None, log=print) -> dict:
    from langfuse import get_client

    usage_ledger.reset()
    train, holdout = load_tasks(skill)  # GEPA optimizes on train; A/B + promotion judge on holdout
    skill_dir = SKILLS_DIR / skill
    if not (skill_dir / "SKILL.md").exists():
        raise SystemExit(f"No skill named '{skill}' in skills/.")
    champion = optimizable_components(skill_dir)  # {description, body} — bundled files preserved on disk

    # 1) GEPA: evolve the full skill (all components) on the TRAIN tasks (judge feedback -> reflection).
    if challenger_file:  # resume: reuse a checkpointed GEPA result, skip straight to the A/B
        ckpt = json.loads(Path(challenger_file).read_text())
        challenger, seed_score, best_score = ckpt["components"], ckpt["seed_score"], ckpt["best_score"]
        budget = ckpt.get("budget", budget)
    elif skip_gepa:  # debug path: A/B the champion against itself
        challenger, seed_score, best_score = champion, 0.0, 0.0
    else:
        log(f"[gepa] optimizing '{skill}' ({len(champion)} components) on {len(train)} train tasks "
            f"(budget {budget} metric calls)…")
        from .gepa_loop import run_gepa
        challenger, seed_score, best_score = run_gepa(champion, train, max_metric_calls=budget)
        log(f"[gepa] inner-loop score: seed {seed_score:.3f} -> best {best_score:.3f}")
        changed = [k for k in champion if challenger.get(k, "").strip() != champion[k].strip()]
        if not changed:
            log("[gepa] no better candidate found — nothing to A/B.")
            return {"skill": skill, "improved": False}
        log(f"[gepa] components changed: {changed}")
        # checkpoint the GEPA result so an A/B failure doesn't cost the whole loop
        ckpt = Path(__file__).resolve().parent.parent / "runs" / f"challenger-{skill}.json"
        ckpt.parent.mkdir(parents=True, exist_ok=True)
        ckpt.write_text(json.dumps({"components": challenger, "seed_score": seed_score,
                                    "best_score": best_score, "budget": budget}, indent=2))
        log(f"[gepa] challenger checkpointed to {ckpt} (resume with --challenger-file)")

    # 2) A/B through the full deep agent on the HELD-OUT tasks (the promotion gate — the optimizer
    #    never saw these), one Langfuse dataset run per variant.
    log(f"[ab] evaluating champion vs challenger on {len(holdout)} held-out tasks…")
    langfuse = get_client()
    ds_name = f"{skill}-holdout"
    langfuse.create_dataset(name=ds_name)
    for i, t in enumerate(holdout):
        langfuse.create_dataset_item(dataset_name=ds_name, id=f"{skill}-holdout-{i}", input=t)
    dataset = langfuse.get_dataset(ds_name)

    mcp_tools = asyncio.run(_connect())
    ts = int(time.time())
    results = {}
    for variant, comps in [("champion", champion), ("challenger", challenger)]:
        run_name = f"{variant}-{ts}"
        log(f"[ab] running variant '{run_name}' through the deep agent ({len(holdout)} held-out tasks)…")
        # A/B serves the variant's description + body (what get_skill returns); bundled-file changes
        # are reviewed in the diff, not executed here (the agent reads files from the on-disk champion).
        agent = build_agent(_variant_tools(mcp_tools, skill, comps["body"], comps["description"]),
                            instructions=EVAL_INSTRUCTIONS)
        scores, usages, behaviors = _run_variant(dataset, run_name, agent, holdout)
        results[variant] = {
            "run": run_name, "scores": scores,
            "mean": statistics.mean(scores) if scores else 0.0,  # all items failing must not crash the run
            "tokens": {
                "input": [u["input_tokens"] for u in usages],
                "output": [u["output_tokens"] for u in usages],
                "mean_input": statistics.mean(u["input_tokens"] for u in usages),
                "mean_output": statistics.mean(u["output_tokens"] for u in usages),
            },
            "behavior": behaviors,
        }
        r = results[variant]
        log(f"[ab] {variant}: mean judge score {r['mean']:.3f}  {scores}")
        log(f"[ab] {variant}: tokens/task in {r['tokens']['mean_input']:.0f} (per-task {r['tokens']['input']})"
            f" | out {r['tokens']['mean_output']:.0f} (per-task {r['tokens']['output']})")
    langfuse.flush()

    wins = results["challenger"]["mean"] > results["champion"]["mean"]
    changed = [k for k in champion if challenger.get(k, "").strip() != champion[k].strip()]
    promotable, blocked = promotion_gate(skill, results["champion"]["scores"],
                                         results["challenger"]["scores"], changed, challenger)
    summary = {
        "skill": skill, "improved": wins, "created": ts,
        "gepa": {"seed_score": seed_score, "best_score": best_score, "budget": budget},
        "ab": {v: {"run": r["run"], "mean": r["mean"], "scores": r["scores"], "tokens": r["tokens"]}
               for v, r in results.items()},
        "dataset": ds_name,
        "gate": {"promotable": promotable, "blocked": blocked},
        "changed_components": changed,
        "champion_components": champion,
        "challenger_components": challenger,
        "harness": "codex",
        "model": os.environ.get("MODEL", "unknown"),
        "behavior": {variant: result["behavior"] for variant, result in results.items()},
    }

    c_tok, ch_tok = results["champion"]["tokens"], results["challenger"]["tokens"]
    out_delta = ch_tok["mean_output"] - c_tok["mean_output"]
    log(f"\n[ab] champion {results['champion']['mean']:.3f} vs challenger "
        f"{results['challenger']['mean']:.3f} -> {'CHALLENGER WINS' if wins else 'champion holds'}")
    # output tokens are the cost that matters (generated every task); input is cheap context
    warn = "  ⚠ output-token regression" if out_delta > 0.1 * c_tok["mean_output"] else ""
    log(f"[ab] output tokens/task: {c_tok['mean_output']:.0f} -> {ch_tok['mean_output']:.0f} "
        f"({out_delta:+.0f}){warn}")
    log(f"[ab] input tokens/task:  {c_tok['mean_input']:.0f} -> {ch_tok['mean_input']:.0f} "
        f"({ch_tok['mean_input'] - c_tok['mean_input']:+.0f}) (informational)")

    summary["optimization_usage"] = usage_ledger.report()
    champion_skill = next(item for item in load_skills() if item.name == skill)
    evidence = build_evidence(summary, champion_skill.revision, component_revision(challenger))
    evidence_root = Path(__file__).resolve().parent.parent / "runs" / "evidence" / skill / str(ts)
    evidence_json, evidence_markdown = write_evidence(evidence, evidence_root)
    summary["evidence"] = evidence
    summary["evidence_paths"] = {"json": str(evidence_json), "markdown": str(evidence_markdown)}
    log(f"[ci] behavioral evidence: {evidence_json} and {evidence_markdown}")
    log("\n[usage] tokens spent by this optimization run:")
    log(usage_ledger.format_report())
    log(f"[ab] compare runs in Langfuse: Datasets -> {ds_name} (http://localhost:3100)")

    # Promotion gate: a mean win alone isn't enough — it must clear the anti-reward-hacking checks.
    if wins and not promotable:
        log(f"[ab] ⛔ challenger won the mean but the promotion gate BLOCKED it: {'; '.join(blocked)}.")
    if not wins:
        log("[ab] champion holds — nothing to promote.")
    elif promote_now and promotable:
        log("[ab] --promote: " + promote(skill, challenger))
    elif promotable:
        p = save_pending(skill, summary)
        log(f"[ab] pending approval written to {p} — review + promote at http://localhost:8080")
    else:  # won the mean but blocked — still record for human review, flagged
        p = save_pending(skill, summary)
        log(f"[ab] blocked challenger recorded (flagged) at {p} for review — NOT auto-promotable.")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("skill")
    ap.add_argument("--promote", action="store_true", help="promote immediately if challenger wins")
    ap.add_argument("--budget", type=int, default=60, help="GEPA max metric calls")
    ap.add_argument("--skip-gepa", action="store_true", help="debug: A/B champion vs itself")
    ap.add_argument("--challenger-file", help="reuse a checkpointed GEPA result, skip to the A/B")
    args = ap.parse_args()
    run_ab(args.skill, promote_now=args.promote, budget=args.budget,
           skip_gepa=args.skip_gepa, challenger_file=args.challenger_file)
