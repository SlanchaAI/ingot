"""Generate a candidate change for one skill and produce the evidence a reviewer needs.

The candidate search (optimize.bestofn) turns the skill's components into a challenger on the train
tasks. Champion and challenger then run through the full agent with a local `route_and_load` on the
held-out tasks; each variant is a Langfuse dataset run (side-by-side in the UI) when the stack is
up. The result is a quarantined record in runs/pending/<skill>.json plus a portable evidence bundle
in runs/evidence/. Nothing here activates anything: promotion is a human action in the UI.

Usage: python -m optimize.ab <skill> [--description] [--candidates N] [--skip-search]
"""
import argparse
import asyncio
import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import yaml
from langchain_core.tools import tool

from agent.run import build_agent, langfuse_config, run_task
from mcp_server.registry import SKILLS_DIR, load_skills, optimizable_components, skill_revision

from . import agent_model, langfuse_available
from . import usage as usage_ledger
from .acceptance import evaluate as acceptance_evaluate, load_criteria as load_acceptance
from .judge import judge
from .promote import save_pending
from .evidence import build_evidence, recorded_path, write_evidence

TASKS_DIR = Path(__file__).resolve().parent / "tasks"

# --- promotion gate (anti reward-hacking / overfitting) ---
PROMOTE_MIN_MARGIN = float(os.environ.get("PROMOTE_MIN_MARGIN", "0.15"))   # mean holdout lift required
# ^ set ABOVE the judge's noise floor: identical skill bodies were observed to differ by ~0.10 on
# judge noise alone, so a smaller margin can promote a challenger that isn't actually better.
PROMOTE_MIN_SAMPLES = int(os.environ.get("PROMOTE_MIN_SAMPLES", "3"))       # min held-out tasks
PASS = float(os.environ.get("PROMOTE_PASS_SCORE", "0.5"))                   # a task "passes" at/above this
COLLISION_SCORE = float(os.environ.get("COLLISION_SCORE", "0.93"))          # route-shadow cutoff
RETENTION_WARN = float(os.environ.get("RETENTION_WARN", "0.5"))             # body-retention review warning
# Which components the candidate search may rewrite. Default: body only, because the description is
# a routing trigger matched by embedding, not instructions the agent reads, so quality search has no
# business there (bare rollouts can't tell the difference and will happily stuff behavior rules into
# it; the full agent then never sees them). Widen with e.g. OPTIMIZE_COMPONENTS=body,file:reference.md
# (bundled text files become mutable AND visible in rollouts) or description,body (the
# routing-regression gate still applies). Caveat for file components: the A/B never executes or
# serves them, so review those diffs by eye, and keep scripts out unless you have
# execution-grounded evals.
OPTIMIZE_COMPONENTS = [c.strip() for c in os.environ.get("OPTIMIZE_COMPONENTS", "body").split(",")
                       if c.strip()]

EVAL_CACHE_DIR = Path(__file__).resolve().parent.parent / "runs" / "eval-cache"


def _champion_cache_key(revision: str, holdout: list[dict]) -> str:
    """Champion holdout results are pure functions of (champion revision, holdout tasks, serving
    model, judge) — cache them so repeat optimize runs only pay for the challenger's side."""
    import hashlib
    payload = json.dumps({"v": 1, "revision": revision, "holdout": holdout, "model": agent_model(),
                          "judge": os.environ.get("JUDGE_MODELS") or os.environ.get("JUDGE_MODEL", "")},
                         sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def optimize_split(champion: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """(mutable seed, frozen context) per OPTIMIZE_COMPONENTS; unknown names fail loudly."""
    unknown = [c for c in OPTIMIZE_COMPONENTS if c not in champion]
    if unknown:
        raise SystemExit(f"OPTIMIZE_COMPONENTS names unknown component(s) {unknown}; "
                         f"skill has {sorted(champion)}")
    seed = {k: v for k, v in champion.items() if k in OPTIMIZE_COMPONENTS}
    frozen = {k: v for k, v in champion.items() if k not in OPTIMIZE_COMPONENTS}
    return seed, frozen


def body_retention(champion_body: str, challenger_body: str) -> float:
    """Fraction of the champion body's non-blank lines that survive (stripped) in the challenger —
    a crude but cheap deletion detector for the review warning."""
    champ = [line.strip() for line in champion_body.splitlines() if line.strip()]
    if not champ:
        return 1.0
    kept = {line.strip() for line in challenger_body.splitlines()}
    return sum(1 for line in champ if line in kept) / len(champ)


def retention_warnings(champion: dict, challenger: dict, changed: list[str], samples: int) -> list[str]:
    """Non-blocking review warnings: a small holdout can't license a large deletion, so a challenger
    that drops most of the champion body gets flagged for the human reviewer (never auto-blocked)."""
    if "body" not in changed:
        return []
    kept = body_retention(champion["body"], challenger.get("body", ""))
    if kept >= RETENTION_WARN:
        return []
    return [f"challenger drops {1 - kept:.0%} of the champion body, gated on only {samples} "
            f"held-out task(s) — review the deletions carefully"]


def _description_shadows(skill: str, new_description: str) -> tuple[str, float]:
    """Nearest OTHER skill to a rewritten description (route-shadow check). ("",0.0) if none."""
    from mcp_server.registry import load_skills
    from mcp_server.router import Router
    others = [s for s in load_skills() if s.name != skill]
    if not others:
        return "", 0.0
    return Router(others).nearest(new_description)


def promotion_gate(skill: str, champ_scores: list[float], chall_scores: list[float],
                   changed: list[str], challenger: dict,
                   routing_failures: list[str] | None = None,
                   leakage: bool = False,
                   routing_metrics: dict | None = None,
                   acceptance_violations: list[str] | None = None) -> tuple[bool, list[str]]:
    """A challenger that merely 'wins the mean' can be reward-hacking a small/noisy eval. Require a
    real margin, enough samples, no per-task catastrophic regression, no acceptance-criteria
    violation on the holdout answers, and no routing-shadow from a rewritten description. Returns
    (promotable, reasons-it-was-blocked)."""
    reasons = list(acceptance_violations or [])
    margin = (statistics.mean(chall_scores) if chall_scores else 0.0) - \
             (statistics.mean(champ_scores) if champ_scores else 0.0)
    if margin < PROMOTE_MIN_MARGIN:
        reasons.append(f"margin {margin:+.2f} < required +{PROMOTE_MIN_MARGIN:.2f}")
    if len(chall_scores) < PROMOTE_MIN_SAMPLES:
        reasons.append(f"only {len(chall_scores)} held-out tasks (< {PROMOTE_MIN_SAMPLES})")
    if leakage:
        reasons.append("holdout reuses training tasks; add an explicit holdout before promotion")
    regressed = [i for i, (c, h) in enumerate(zip(champ_scores, chall_scores)) if c >= PASS and h < PASS]
    if regressed:
        reasons.append(f"catastrophic regression on {len(regressed)} task(s) the champion passed")
    if "description" in changed:
        shadowed, score = _description_shadows(skill, challenger["description"])
        if score >= COLLISION_SCORE:
            reasons.append(f"rewritten description shadows '{shadowed}' (cosine {score:.2f}) — routing hack")
        if routing_failures:
            reasons.append(f"routing regression on {len(routing_failures)} held-out task(s)")
        if routing_metrics is None:
            reasons.append("description changed without a routing suite")
        else:
            champion_route = routing_metrics["champion"]
            challenger_route = routing_metrics["challenger"]
            if challenger_route["recall_at_3"] < 0.95:
                reasons.append(f"routing recall@3 {challenger_route['recall_at_3']:.3f} < 0.950")
            if challenger_route["no_route_precision"] < 0.95:
                reasons.append(f"no-route precision {challenger_route['no_route_precision']:.3f} < 0.950")
            for metric in ("top1", "recall_at_3", "no_route_precision"):
                if challenger_route[metric] < champion_route[metric]:
                    reasons.append(f"routing {metric} regressed {champion_route[metric]:.3f} -> "
                                   f"{challenger_route[metric]:.3f}")
            parity = routing_metrics["parity"]
            if parity["total"] and parity["rate"] < 1.0:
                reasons.append(f"cross-harness parity {parity['rate']:.3f} < 1.000")
    return (not reasons), reasons

EVAL_INSTRUCTIONS = """You are a deep agent with access to a read-only skill router over MCP.
For every task, call `route_and_load` once with the full task, harness `codex`, current working
directory, and available tools/MCPs. Follow `skill_body` for a direct `match`. For a
`related_match`, use its loaded `skill_body` as a starting point to compose or extend. Only solve
directly when neither is returned. Never request a skill catalog. Keep the final answer concise.
Your final answer must contain the complete deliverable itself — e.g. full runnable code inline —
never just a description of, or reference to, files you created in your workspace: the user cannot
see your workspace."""

# The quality A/B injects the variant body directly: the experiment compares BODIES, so serving
# must be guaranteed — a model that skips the routing tool for easy-looking tasks would otherwise
# silently turn both arms into identical no-skill baselines (observed: zero tool calls, both
# variants' input tokens identical to the digit). Routing fidelity is the description pass's job.
# The template itself is shared with the candidate search's rollouts (optimize.SERVE_TEMPLATE) so
# the search optimizes against the exact contract this A/B serves.
from . import SERVE_TEMPLATE as EVAL_SERVE_TEMPLATE  # noqa: E402


def load_tasks(skill: str, log=print) -> tuple[list[dict], list[dict], dict]:
    """Return train, holdout, and split metadata. The candidate search sees train; the gate is
    judged on holdout — a leakage-clean split so a challenger has to *generalize*, not memorize.
    A flat `tasks:` list is marked leaky and cannot produce a promotable gate. If no task set exists,
    the teacher drafts one; that draft must include a real holdout before promotion."""
    p = TASKS_DIR / f"{skill}.yaml"
    if not p.exists():
        from mcp_server.registry import SKILLS_DIR as _SD, read_components
        comps = read_components(_SD / skill)
        from .draft import draft_and_save
        draft_and_save(skill, comps["description"], comps["body"], TASKS_DIR, log=log)
    data = yaml.safe_load(p.read_text())
    train = data.get("train") or data.get("tasks") or []
    explicit_holdout = bool(data.get("holdout"))
    holdout = data.get("holdout") or train
    split = {"kind": "holdout" if explicit_holdout else "none", "leakage": not explicit_holdout}
    return train, holdout, split


def _variant_tools(skill: str, body: str, description: str):
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


def _routing_failures(skill: str, challenger: dict, tasks: list[dict]) -> list[str]:
    from mcp_server.router import Router
    skills = load_skills()
    variants = [replace(item, description=challenger["description"], body=challenger["body"])
                if item.name == skill else item for item in skills]
    router = Router(variants)
    return [task["task"] for task in tasks
            if router.route(task["task"], "codex", os.getcwd()).get("match") != skill]


def _routing_metrics(skill: str, champion: dict, challenger: dict) -> dict | None:
    data = yaml.safe_load((TASKS_DIR / f"{skill}.yaml").read_text()) or {}
    cases = data.get("routing") or []
    if not cases:
        return None
    from mcp_server.router import Router
    from mcp_server.routing_eval import evaluate_cases, evaluate_parity
    skills = load_skills()

    def variant(components):
        return Router([replace(item, description=components["description"], body=components["body"])
                       if item.name == skill else item for item in skills])

    champion_router, challenger_router = variant(champion), variant(challenger)
    return {"champion": evaluate_cases(champion_router, cases),
            "challenger": evaluate_cases(challenger_router, cases),
            "parity": evaluate_parity(challenger_router, cases)}


def _run_variant(dataset, variant: str, agent, tasks: list[dict]):
    """One Langfuse dataset run (experiment) for a variant; returns (judge scores, per-task token usage)."""
    from langfuse import Evaluation

    # keyed by task text so scores and usages stay aligned per-task regardless of the order
    # run_experiment processes items in; a failed item defaults to 0
    scores_by_task: dict[str, float] = {}
    usage_by_task: dict[str, dict] = {}
    behavior_by_task: dict[str, list[dict]] = {}
    answer_by_task: dict[str, str] = {}

    async def task_fn(*, item, **kwargs):
        # run_experiment drives its own event loop and awaits async tasks
        answer, _, usage, behavior = await run_task(
            agent, item.input["task"], config=langfuse_config(tags=[f"variant={variant}"]),
            include_behavior=True)
        usage_by_task[item.input["task"]] = usage
        behavior_by_task[item.input["task"]] = behavior
        answer_by_task[item.input["task"]] = str(answer)
        usage_ledger.add("agent_ab", usage)
        return answer

    def judge_evaluator(*, input, output, **kwargs):
        j = judge(input["task"], input["rubric"], str(output), check=input.get("check"),
                  deliverable=input.get("deliverable"))
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
            [behavior_by_task.get(t["task"], []) for t in tasks],
            [answer_by_task.get(t["task"], "") for t in tasks])


def _run_variant_local(agent, tasks: list[dict]):
    """Langfuse-free twin of _run_variant: identical rollouts and judging, no experiment
    logging. Used when the tracing stack isn't running (lite mode); a failed item scores 0,
    matching _run_variant's per-item default."""
    async def rollout_all():
        return await asyncio.gather(
            *[run_task(agent, t["task"], config={}, include_behavior=True) for t in tasks],
            return_exceptions=True)
    outs = asyncio.run(rollout_all())
    ok = [(i, t, o) for i, (t, o) in enumerate(zip(tasks, outs))
          if not isinstance(o, BaseException)]
    for _, _, o in ok:
        usage_ledger.add("agent_ab", o[2])
    scores = [0.0] * len(tasks)
    with ThreadPoolExecutor(max_workers=max(1, len(ok))) as pool:
        judgments = list(pool.map(
            lambda x: judge(x[1]["task"], x[1]["rubric"], str(x[2][0]),
                            check=x[1].get("check"), deliverable=x[1].get("deliverable")), ok))
    for (i, _, _), j in zip(ok, judgments):
        scores[i] = j["score"]
    zero = {"input_tokens": 0, "output_tokens": 0}
    return (scores,
            [zero if isinstance(o, BaseException) else o[2] for o in outs],
            [[] if isinstance(o, BaseException) else o[3] for o in outs],
            ["" if isinstance(o, BaseException) else str(o[0]) for o in outs])


def _eval_variants(dataset, ts: int, champion: dict, challenger: dict, holdout: list[dict],
                   cache_path: Path, log=print) -> dict:
    """Champion and challenger holdout experiments, concurrently when both run live. The
    champion side is served from (and recorded to) the revision-keyed eval cache."""
    def eval_variant(variant: str, comps: dict) -> dict:
        run_name = f"{variant}-{ts}"
        log(f"[ab] running variant '{run_name}' through the deep agent ({len(holdout)} held-out tasks)…")
        # Guaranteed serving: the variant body is injected into the instructions (see
        # EVAL_SERVE_TEMPLATE) instead of hoping the model fetches it through a tool call.
        agent = build_agent([], instructions=EVAL_SERVE_TEMPLATE.format(body=comps["body"]))
        scores, usages, behaviors, answers = (_run_variant(dataset, run_name, agent, holdout)
                                              if dataset is not None
                                              else _run_variant_local(agent, holdout))
        return {
            "run": run_name, "scores": scores,
            "mean": statistics.mean(scores) if scores else 0.0,  # all items failing must not crash the run
            "tokens": {
                "input": [u["input_tokens"] for u in usages],
                "output": [u["output_tokens"] for u in usages],
                "mean_input": statistics.mean(u["input_tokens"] for u in usages),
                "mean_output": statistics.mean(u["output_tokens"] for u in usages),
            },
            "behavior": behaviors,
            "answers": answers,
        }

    results = {}
    variants = [("champion", champion), ("challenger", challenger)]
    if cache_path.exists():
        # the champion side is deterministic in (revision, holdout, model, judge) up to judge
        # noise — reuse the recorded run instead of re-spending the full agent + judge on it
        results["champion"] = json.loads(cache_path.read_text())
        log(f"[ab] champion: holdout results reused from cache (revision unchanged) — "
            f"mean {results['champion']['mean']:.3f}  {results['champion']['scores']}")
        variants = [("challenger", challenger)]
    if len(variants) == 2:
        # the two variants are independent: run both dataset experiments concurrently, which
        # halves the cold gate's wall-clock (the cached-champion path is one run anyway)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {v: pool.submit(eval_variant, v, comps) for v, comps in variants}
            for v, _ in variants:
                results[v] = futures[v].result()
    else:
        for v, comps in variants:
            results[v] = eval_variant(v, comps)
    if "champion" in dict(variants):
        EVAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(results["champion"]))
    for variant in ("champion", "challenger"):
        r = results[variant]
        log(f"[ab] {variant}: mean judge score {r['mean']:.3f}  {r['scores']}")
        log(f"[ab] {variant}: tokens/task in {r['tokens']['mean_input']:.0f} (per-task {r['tokens']['input']})"
            f" | out {r['tokens']['mean_output']:.0f} (per-task {r['tokens']['output']})")
    return results


def _holdout_dataset(skill: str, holdout: list[dict], log):
    """(langfuse client, dataset) for the holdout when the stack is reachable, else
    (None, None): the gate then runs locally with no experiment logging (lite mode)."""
    if not langfuse_available():
        log("[ab] Langfuse unreachable — running the holdout gate locally (no experiment logging)")
        return None, None
    from langfuse import get_client
    langfuse = get_client()
    ds_name = f"{skill}-holdout"
    langfuse.create_dataset(name=ds_name)
    for i, t in enumerate(holdout):
        langfuse.create_dataset_item(dataset_name=ds_name, id=f"{skill}-holdout-{i}", input=t)
    return langfuse, langfuse.get_dataset(ds_name)


def run_ab(skill: str, skip_search: bool = False, challenger_file: str | None = None,
           log=print) -> dict:
    usage_ledger.reset()
    train, holdout, split = load_tasks(skill)
    skill_dir = SKILLS_DIR / skill
    if not (skill_dir / "SKILL.md").exists():
        raise SystemExit(f"No skill named '{skill}' in skills/.")
    # description + body always; bundled file components join only when OPTIMIZE_COMPONENTS names
    # them (they then also render into rollouts). Everything else stays untouched on disk.
    champion = optimizable_components(skill_dir)
    file_components = [c for c in OPTIMIZE_COMPONENTS if c.startswith("file:")]
    if file_components:
        from mcp_server.registry import read_components
        everything = read_components(skill_dir)
        champion.update({k: everything[k] for k in file_components if k in everything})

    # 1) Candidate search: draft a challenger from the mutable components on the TRAIN tasks.
    if challenger_file:  # resume: reuse a checkpointed candidate, skip straight to the A/B
        ckpt = json.loads(Path(challenger_file).read_text())
        challenger, seed_score, best_score = ckpt["components"], ckpt["seed_score"], ckpt["best_score"]
    elif skip_search:  # debug path: A/B the champion against itself
        challenger, seed_score, best_score = champion, 0.0, 0.0
    else:
        seed, frozen = optimize_split(champion)
        # rollouts see the frozen description (get_skill serves it) but not frozen files — those
        # are neither mutated nor measured, and pasting them in would only inflate rollout cost
        rollout_frozen = {k: v for k, v in frozen.items() if k == "description"}
        log(f"[skillopt] searching candidates for '{skill}' (components: {sorted(seed)}; frozen: "
            f"{sorted(frozen)}) on {len(train)} train tasks (SkillOpt reflective training loop)…")
        from .skillopt_loop import run_skillopt
        # the skill's acceptance criteria become a training signal so the loop removes forbidden
        # content instead of appending around it (the promotion gate still enforces them on holdout)
        best, seed_score, best_score = run_skillopt(seed, train, frozen=rollout_frozen,
                                                    acceptance=load_acceptance(skill, TASKS_DIR), log=log)
        challenger = {**champion, **best}
        log(f"[opt] candidate search score: seed {seed_score:.3f} -> best {best_score:.3f}")
        changed = [k for k in champion if challenger.get(k, "").strip() != champion[k].strip()]
        if not changed:
            log("[opt] no better candidate found — nothing to A/B.")
            return {"skill": skill, "improved": False}
        log(f"[opt] components changed: {changed}")
        # checkpoint the candidate so an A/B failure doesn't cost the whole search
        ckpt = Path(__file__).resolve().parent.parent / "runs" / f"challenger-{skill}.json"
        ckpt.parent.mkdir(parents=True, exist_ok=True)
        ckpt.write_text(json.dumps({"components": challenger, "seed_score": seed_score,
                                    "best_score": best_score}, indent=2))
        log(f"[opt] challenger checkpointed to {ckpt} (resume with --challenger-file)")

    # 2) A/B through the full deep agent on the HELD-OUT tasks (the evidence the gate reads; the
    #    search never saw these), one Langfuse dataset run per variant when the stack is up, or the
    #    Langfuse-free local path when it isn't (same rollouts and judge, no experiment logging).
    log(f"[ab] evaluating champion vs challenger on {len(holdout)} held-out tasks…")
    ds_name = f"{skill}-holdout"
    langfuse, dataset = _holdout_dataset(skill, holdout, log)

    ts = int(time.time())
    champion_skill = next(item for item in load_skills() if item.name == skill)
    cache_path = EVAL_CACHE_DIR / f"{skill}-{_champion_cache_key(champion_skill.revision, holdout)}.json"
    results = _eval_variants(dataset, ts, champion, challenger, holdout, cache_path, log)
    if langfuse is not None:
        langfuse.flush()

    wins = results["challenger"]["mean"] > results["champion"]["mean"]
    changed = [k for k in champion if challenger.get(k, "").strip() != champion[k].strip()]
    route_failures = _routing_failures(skill, challenger, holdout) if "description" in changed else []
    route_metrics = _routing_metrics(skill, champion, challenger) if "description" in changed else None
    # Deterministic invariants on the challenger's holdout answers — a hard gate that the LLM
    # judge's mean score can't override (grounds the judge the same way execcheck does).
    acceptance_violations = acceptance_evaluate(
        load_acceptance(skill, TASKS_DIR), results["challenger"].get("answers", []))
    promotable, blocked = promotion_gate(skill, results["champion"]["scores"],
                                         results["challenger"]["scores"], changed, challenger,
                                         routing_failures=route_failures, leakage=split["leakage"],
                                         routing_metrics=route_metrics,
                                         acceptance_violations=acceptance_violations)
    if acceptance_violations:
        log(f"[ab] ⛔ acceptance criteria violated: {'; '.join(acceptance_violations)}")
    warnings = retention_warnings(champion, challenger, changed, len(results["challenger"]["scores"]))
    summary = {
        "skill": skill, "improved": wins, "created": ts,
        "inner_loop": {"seed_score": seed_score, "best_score": best_score},
        "ab": {v: {"run": r["run"], "mean": r["mean"], "scores": r["scores"], "tokens": r["tokens"]}
               for v, r in results.items()},
        "dataset": ds_name,
        "gate": {"promotable": promotable, "blocked": blocked, "warnings": warnings,
                 "acceptance_violations": acceptance_violations},
        "changed_components": changed,
        "champion_components": champion,
        "challenger_components": challenger,
        "harness": "codex",
        "model": agent_model(),
        "judge": os.environ.get("JUDGE_MODELS") or os.environ.get("JUDGE_MODEL", ""),
        "behavior": {variant: result["behavior"] for variant, result in results.items()},
        "routing_failures": route_failures,
        "routing": route_metrics,
        "split": split,
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
    evidence = build_evidence(summary, champion_skill.revision,
                              skill_revision(Path(champion_skill.root), challenger))
    evidence_root = Path(__file__).resolve().parent.parent / "runs" / "evidence" / skill / str(ts)
    evidence_json, evidence_markdown = write_evidence(evidence, evidence_root)
    summary["evidence"] = evidence
    summary["evidence_paths"] = {"json": recorded_path(evidence_json),
                                 "markdown": recorded_path(evidence_markdown)}
    log(f"[ci] behavioral evidence: {evidence_json} and {evidence_markdown}")
    log("\n[usage] tokens spent by this candidate run:")
    log(usage_ledger.format_report())
    if langfuse is not None:
        log(f"[ab] compare runs in Langfuse: Datasets -> {ds_name} (http://localhost:3100)")

    # Promotion gate: a mean win alone isn't enough — it must clear the anti-reward-hacking checks.
    if wins and not promotable:
        log(f"[ab] ⛔ challenger won the mean but the promotion gate BLOCKED it: {'; '.join(blocked)}.")
    if warnings:
        log(f"[ab] ⚠ {'; '.join(warnings)}")
    if not wins:
        log("[ab] champion holds — nothing to promote.")
    elif promotable:
        p = save_pending(skill, summary)
        log(f"[ab] pending approval written to {p} — review + promote at http://localhost:8080")
    else:  # won the mean but blocked — still record for human review, flagged
        p = save_pending(skill, summary)
        log(f"[ab] blocked challenger recorded (flagged) at {p} for review — NOT auto-promotable.")
    return summary


DEFAULT_ROUTING_BUDGET = 60


def build_parser() -> argparse.ArgumentParser:
    """The candidate-generation CLI. Kept out of `__main__` so its rejections are testable: a flag
    for a pass that does not exist has to fail loudly, not be quietly accepted or ignored."""
    ap = argparse.ArgumentParser(prog="python -m optimize.ab")
    ap.add_argument("skill")
    passes = ap.add_mutually_exclusive_group()
    passes.add_argument("--body", action="store_true",
                        help="quality pass over the SKILL.md body (the default)")
    passes.add_argument("--description", action="store_true",
                        help="routing pass over the description (embedding-scored inner loop)")
    ap.add_argument("--budget", type=int, default=None,
                    help=f"--description only: max GEPA metric calls for the routing pass "
                         f"(default {DEFAULT_ROUTING_BUDGET})")
    ap.add_argument("--skip-search", action="store_true", help="debug: A/B champion vs itself")
    ap.add_argument("--challenger-file", help="reuse a checkpointed candidate, skip to the A/B")
    return ap


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Reject a --budget the chosen pass would ignore. The body pass has no metric-call budget, so
    accepting the flag there would silently promise a limit that nothing enforces."""
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.budget is not None and not args.description:
        ap.error("--budget applies to the routing pass only; add --description or drop --budget")
    args.budget = DEFAULT_ROUTING_BUDGET if args.budget is None else args.budget
    return args


if __name__ == "__main__":
    args = parse_args()
    from . import require_openrouter_key
    require_openrouter_key()
    if args.description:
        from .routing import run_routing
        run_routing(args.skill, budget=args.budget)
    else:
        run_ab(args.skill, skip_search=args.skip_search, challenger_file=args.challenger_file)
