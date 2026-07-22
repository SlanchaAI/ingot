"""SkillOpt routing pass over a skill's `description`.

Each candidate description is scored by the real embedding router against the skill's `routing:`
cases. SkillOpt reflects on the per-case failures, proposes bounded edits, and keeps only strict
improvements. The promotion gate requires no routing regression, at least one strict improvement,
and no collision with another skill description."""
import os
import time
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

import yaml

from mcp_server.registry import SKILLS_DIR, load_skills, optimizable_components, skill_revision
from mcp_server.router import Router

from . import usage as usage_ledger
from .ab import COLLISION_SCORE, TASKS_DIR, _description_shadows
from .evidence import RoutingRun, build_routing_evidence, recorded_path, write_evidence
from .promote import save_pending
from .rollout import make_reflection_lm
from . import skillopt_bridge as sk

EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "runs" / "evidence"

_DIAGNOSIS = ("The `description` is a routing trigger matched by embedding similarity against the "
              "user's task. Adjust trigger phrases so expected tasks match and unrelated ones "
              "don't; keep it a concise 'Use this skill when…' summary, never behavioral "
              "instructions.")


@dataclass
class RoutingBatch:
    outputs: list
    scores: list[float]
    trajectories: list[dict] | None


class RoutingAdapter:
    """Score a description with the real router: exact is 1, top-three is 0.5, miss is 0."""

    def __init__(self, skill: str, router_factory=None):
        self._skill = skill
        self._factory = router_factory or self._variant_router

    def _variant_router(self, description: str) -> Router:
        return Router([replace(item, description=description) if item.name == self._skill else item
                       for item in load_skills()])

    def evaluate(self, batch, candidate, capture_traces=False):
        router = self._factory(candidate["description"])
        outputs, scores, trajectories = [], [], []
        for case in batch:
            expected = case.get("expected")
            result = router.route(case["task"], case.get("harness", "codex"), case.get("cwd", "."),
                                  case.get("available_tools", []), case.get("available_mcps", []))
            match = result.get("match")
            ranked = [match] + [a["name"] for a in result.get("alternatives", [])]
            if expected is None:
                score = 1.0 if match is None else 0.0
                feedback = ("correctly matched no skill" if score else
                            f"matched '{match}' but this task should route to NO skill, the "
                            f"description triggers too broadly")
            elif match == expected:
                score, feedback = 1.0, f"routed to '{expected}' as expected"
            elif expected in ranked[:3]:
                score = 0.5
                feedback = (f"expected '{expected}' but it ranked behind '{match}', sharpen the "
                            f"description's trigger phrases for this kind of task")
            else:
                score = 0.0
                feedback = (f"expected '{expected}' but routed to '{match}' and the expected skill "
                            f"is not in the top 3, the description is missing this task's "
                            f"trigger phrasing")
            outputs.append(match)
            scores.append(score)
            trajectories.append({"task": case["task"], "output": str(match), "feedback": feedback})
        return RoutingBatch(outputs, scores, trajectories if capture_traces else None)


def _score(batch: RoutingBatch) -> float:
    return sum(batch.scores) / len(batch.scores) if batch.scores else 0.0


def _hard_score(batch: RoutingBatch) -> float:
    return (sum(score == 1.0 for score in batch.scores) / len(batch.scores)
            if batch.scores else 0.0)


def optimize_description(skill: str, seed: str, cases: list[dict], budget: int,
                         reflection_lm=None, adapter=None) -> tuple[str, float, float]:
    """Train a routing description with bounded SkillOpt edits and a strict router-score gate.

    `budget` caps router case evaluations. The seed consumes one full evaluation, and each proposed
    revision consumes another. Rejected edits and routing failures are fed into the next reflection.
    """
    if cases and budget < len(cases):
        raise ValueError(f"routing budget {budget} is smaller than the {len(cases)}-case suite; "
                         "one complete suite evaluation is required")
    adapter = adapter or RoutingAdapter(skill)
    reflection_lm = reflection_lm or make_reflection_lm()
    max_edits = int(os.environ.get("SKILLOPT_MAX_EDITS", "3"))
    current = best = seed
    current_batch = adapter.evaluate(cases, {"description": current}, capture_traces=True)
    seed_score = best_score = _score(current_batch)
    remaining = max(0, budget - len(cases))
    buffer: list[str] = []

    while remaining >= len(cases):
        failures = [trajectory for trajectory, score in
                    zip(current_batch.trajectories or [], current_batch.scores) if score < 1.0]
        if not failures:
            break
        context = _DIAGNOSIS
        if buffer:
            context += "\n\nRejected edits from prior steps:\n" + "\n".join(buffer[-4:])
        edits, _summary = sk.reflect_edits(current, failures, context, max_edits, reflection_lm)
        if not edits:
            break
        edit_budget = sk.decide_edit_budget(current, edits, _hard_score(current_batch), best_score,
                                             len(cases),
                                             context, reflection_lm, max_edits)
        edits = sk.rank_edits(current, edits, edit_budget, reflection_lm) if edit_budget else []
        candidate, _report = sk.apply_edits(current, edits)
        if not edits or candidate.strip() == current.strip():
            break
        candidate_batch = adapter.evaluate(cases, {"description": candidate}, capture_traces=True)
        remaining -= len(cases)
        candidate_score = _score(candidate_batch)
        if candidate_score > best_score:
            current = best = candidate.strip()
            current_batch = candidate_batch
            best_score = candidate_score
        else:
            buffer.extend(
                f"{edit.get('op', 'edit')} target={edit.get('target', '')[:80]!r}"
                for edit in edits)
    return best, seed_score, best_score


def routing_gate(skill: str, metrics: dict, challenger: dict) -> tuple[bool, list[str]]:
    """No regression on any routing metric, at least one strict improvement, no route-shadowing."""
    champ, chall = metrics["champion"], metrics["challenger"]
    reasons, improved = [], False
    for metric in ("top1", "recall_at_3", "no_route_precision"):
        if chall[metric] < champ[metric]:
            reasons.append(f"routing {metric} regressed {champ[metric]:.3f} -> {chall[metric]:.3f}")
        elif chall[metric] > champ[metric]:
            improved = True
    parity = metrics.get("parity") or {}
    if parity.get("total") and parity["rate"] < 1.0:
        reasons.append(f"cross-harness parity {parity['rate']:.3f} < 1.000")
    shadowed, score = _description_shadows(skill, challenger["description"])
    if score >= COLLISION_SCORE:
        reasons.append(f"rewritten description shadows '{shadowed}' (cosine {score:.2f})")
    if not improved and not reasons:
        reasons.append("no routing metric improved, nothing to gain from this change")
    return (not reasons), reasons


def run_routing(skill: str, budget: int = 60, log=print) -> dict:
    usage_ledger.reset()
    skill_dir = SKILLS_DIR / skill
    if not (skill_dir / "SKILL.md").exists():
        raise SystemExit(f"No skill named '{skill}' in skills/.")
    tasks_path = TASKS_DIR / f"{skill}.yaml"
    cases = (yaml.safe_load(tasks_path.read_text()) or {}).get("routing") if tasks_path.exists() else None
    champion = optimizable_components(skill_dir)
    if not cases:
        # auto-draft like the task drafter does, persisted, so re-runs use the same suite
        from .draft import draft_and_append_routing
        cases = draft_and_append_routing(skill, champion["description"], champion["body"],
                                         TASKS_DIR, log=log)

    log(f"[routing] optimizing '{skill}' description against {len(cases)} routing cases "
        f"(budget {budget} router case evaluations; SkillOpt reflection, no LLM rollouts)…")
    description, seed_score, best_score = optimize_description(
        skill, champion["description"], cases, budget)
    log(f"[routing] inner-loop score: seed {seed_score:.3f} -> best {best_score:.3f}")

    challenger = {**champion, "description": description}
    if challenger["description"].strip() == champion["description"].strip():
        log("[routing] no better description found, champion holds.")
        return {"skill": skill, "improved": False}

    from .ab import _routing_metrics
    metrics = _routing_metrics(skill, champion, challenger)
    promotable, blocked = routing_gate(skill, metrics, challenger)
    for variant in ("champion", "challenger"):
        m = metrics[variant]
        log(f"[routing] {variant}: top1 {m['top1']:.3f} · recall@3 {m['recall_at_3']:.3f} · "
            f"no-route precision {m['no_route_precision']:.3f}")
    if not promotable:
        log(f"[routing] ⛔ gate blocked the new description: {'; '.join(blocked)}")

    current = next(item for item in load_skills() if item.name == skill)
    challenger_revision = skill_revision(Path(current.root), challenger)
    gate = {"promotable": promotable, "blocked": blocked, "warnings": []}
    inner_loop = {"seed_score": seed_score, "best_score": best_score, "budget": budget}
    created = int(time.time())
    dataset = f"{skill}-routing"

    # The same portable bundle the body pass writes, so a reviewer reads routing changes and
    # quality changes from one place instead of trusting a claim about one of them.
    evidence = build_routing_evidence(RoutingRun(
        skill=skill, created=created, dataset=dataset, metrics=metrics,
        champion_revision=current.revision, challenger_revision=challenger_revision,
        inner_loop=inner_loop, gate=gate))
    evidence_json, evidence_markdown = write_evidence(evidence, EVIDENCE_DIR / skill / str(created))
    log(f"[ci] routing evidence: {evidence_json} and {evidence_markdown}")

    pending = {
        "skill": skill, "kind": "routing", "improved": promotable, "created": created,
        "inner_loop": inner_loop,
        "routing": metrics, "dataset": dataset, "gate": gate,
        "changed_components": ["description"],
        "champion_components": champion, "challenger_components": challenger,
        "evidence": {"champion": {"revision": current.revision},
                     "challenger": {"revision": challenger_revision},
                     "gate": gate},
        "evidence_paths": {"json": recorded_path(evidence_json),
                           "markdown": recorded_path(evidence_markdown)},
    }
    path = save_pending(skill, pending)
    log(f"[routing] pending description written to {path}, review + promote at http://localhost:8080")
    log("\n[usage] tokens spent by this routing pass (reflection only):")
    log(usage_ledger.format_report())
    return pending
