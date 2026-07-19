"""Body-pass optimizer — SkillOpt's reflective training loop (microsoft/SkillOpt, MIT, v0.2.0)
adapted to skill_router's rollout + judge.

Treats the skill `body` as trainable state and improves it with SkillOpt's four disciplines,
each driven through `optimize.skillopt_bridge` (the single dependency seam):

  1. a step buffer of prior failures + *rejected* edits, fed back into each reflection so the
     optimizer stops re-proposing what the gate already threw out;
  2. bounded patch edits (append / insert_after / replace / delete) clipped to a top-L budget,
     instead of full-body rewrites — minimal, reviewable diffs;
  3. an autonomous learning-rate (the optimizer picks how many edits to apply per step) plus an
     epoch-end slow/meta consolidation over the whole epoch's longitudinal change;
  4. a held-out gate on a hard/soft/mixed metric that only accepts a strictly-improving candidate.

Drop-in for the inner loop: `run_skillopt(seed, tasks, frozen, ...) -> (best_components, seed_score,
best_score)`, scores being the penalized mean judge over the train set (comparable to what the outer
A/B logs). The leakage-clean held-out promotion gate in optimize.ab is unchanged and remains the
promotion authority; this only replaces how the challenger body is produced.
"""
import os
from concurrent.futures import ThreadPoolExecutor

from . import SERVE_TEMPLATE
from . import skillopt_bridge as sk
from .rollout import SkillAdapter, assemble, length_penalty, make_reflection_lm

_MAX_WORKERS = 16
PASS = float(os.environ.get("PROMOTE_PASS_SCORE", "0.5"))          # a task "passes" (hard) at/above this
_SLOW_START, _SLOW_END = "<!-- SLOW_UPDATE_START -->", "<!-- SLOW_UPDATE_END -->"
_BUFFER_KEEP = 4                                                   # most-recent steps shown to reflection


def _format_buffer(buffer: list[dict]) -> str:
    """Render the tail of the step buffer for the reflection prompt: recurring failure patterns and
    the edits the gate has already rejected (so the optimizer doesn't re-propose them)."""
    lines = []
    for entry in buffer[-_BUFFER_KEEP:]:
        for f in entry.get("failures") or []:
            desc = f.get("description") or f.get("failure_type") or str(f)
            lines.append(f"- recurring failure: {desc}")
        for e in entry.get("rejected") or []:
            lines.append(f"- REJECTED edit (did not improve the held-out score): {sk._describe(e)}")
    return "\n".join(lines)


def _longitudinal(tasks: list[dict], before: dict, after: dict) -> str:
    """Categorize the same tasks under the epoch's start vs end skill for the slow-update prompt."""
    buckets = {"regressions": [], "persistent failures": [], "improvements": [], "stable successes": []}
    for t in tasks:
        key = t["task"]
        b, a = before.get(key, False), after.get(key, False)
        bucket = ("stable successes" if b and a else "improvements" if a and not b
                  else "regressions" if b and not a else "persistent failures")
        buckets[bucket].append(key)
    return "\n".join(f"{name} ({len(v)}): " + "; ".join(x[:60] for x in v) for name, v in buckets.items())


def _inject_slow_update(doc: str, guidance: str) -> str:
    """Write consolidated epoch guidance into the protected slow-update region (replacing any prior
    one). apply_patch_with_report treats this region as read-only for step edits."""
    block = f"{_SLOW_START}\n## Consolidated guidance\n{guidance}\n{_SLOW_END}"
    if _SLOW_START in doc and _SLOW_END in doc:
        head, rest = doc.split(_SLOW_START, 1)
        _, tail = rest.split(_SLOW_END, 1)
        return f"{head.rstrip()}\n\n{block}\n{tail.lstrip()}"
    return f"{doc.rstrip()}\n\n{block}\n"


def run_skillopt(seed: dict[str, str], tasks: list[dict], frozen: dict[str, str] | None = None,
                 budget: int | None = None, log=print) -> tuple[dict[str, str], float, float]:
    if not tasks:
        log("[skillopt] no train tasks — keeping the seed.")
        return seed, 0.0, 0.0
    epochs = int(os.environ.get("SKILLOPT_EPOCHS", "2"))
    batch_size = int(os.environ.get("SKILLOPT_MINIBATCH", "3"))
    max_edits = int(os.environ.get("SKILLOPT_MAX_EDITS", "3"))
    doc_key = "body" if "body" in seed else next(iter(seed))
    adapter = SkillAdapter(frozen)
    reflection_lm = make_reflection_lm()

    def rollout_all(doc: str, subset: list[dict]) -> list:
        comps = {**seed, doc_key: doc}
        system = SERVE_TEMPLATE.format(body=assemble({**(frozen or {}), **comps}))
        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(subset))) as pool:
            return list(pool.map(lambda ex: adapter._rollout(system, ex), subset))

    def evaluate(doc: str) -> tuple[float, float, dict]:
        """(hard pass-rate, penalized soft mean, per-task pass map) over the full train set — the
        held-out selection signal for the inner gate."""
        rs = rollout_all(doc, tasks)
        soft = max(0.0, sum(r[1] for r in rs) / len(rs) - length_penalty(doc))
        hard = sum(1.0 for r in rs if r[1] >= PASS) / len(rs)
        passes = {tasks[i]["task"]: rs[i][1] >= PASS for i in range(len(tasks))}
        return hard, soft, passes

    current, (c_hard, c_soft, c_pass) = seed[doc_key], evaluate(seed[doc_key])
    seed_gate, seed_score = sk.score(c_hard, c_soft), c_soft
    current_score = seed_gate
    best, best_soft, best_score, best_step = current, c_soft, current_score, 0
    log(f"[skillopt] seed: hard {c_hard:.3f} soft {c_soft:.3f} gate {current_score:.3f} "
        f"({sk.gate_metric()}) on {len(tasks)} train tasks; {epochs} epoch(s), "
        f"minibatch {batch_size}, ≤{max_edits} edits/step")
    buffer: list[dict] = []
    minibatches = [tasks[i:i + batch_size] for i in range(0, len(tasks), batch_size)]
    step = 0

    for epoch in range(epochs):
        epoch_start_doc, epoch_start_pass, accepted = current, c_pass, False
        for mb in minibatches:
            step += 1
            rolls = rollout_all(current, mb)
            failing = [r[2] for r in rolls if r[1] < 1.0]
            if not failing:
                continue
            buf_ctx = _format_buffer(buffer)
            edits, summary = sk.reflect_edits(current, failing, buf_ctx, max_edits, reflection_lm)
            if not edits:
                buffer.append({"failures": summary, "rejected": []})
                continue
            mb_hard = sum(1.0 for r in rolls if r[1] >= PASS) / len(rolls)
            mb_soft = sum(r[1] for r in rolls) / len(rolls)
            L = sk.decide_edit_budget(current, edits, mb_hard, mb_soft, len(mb), buf_ctx,
                                      reflection_lm, max_edits)
            if L == 0:
                buffer.append({"failures": summary, "rejected": edits})
                continue
            edits = sk.rank_edits(current, edits, L, reflection_lm)
            cand, _report = sk.apply_edits(current, edits)
            if cand.strip() == current.strip():
                continue
            h, s, _ = evaluate(cand)
            decision = sk.evaluate_gate(cand, h, current, current_score, best, best_score, best_step,
                                        step, cand_soft=s, metric=sk.gate_metric(),
                                        mixed_weight=sk.gate_mixed_weight())
            if decision.action in ("accept", "accept_new_best"):
                current, current_score = decision.current_skill, decision.current_score
                if decision.action == "accept_new_best":
                    best, best_soft, best_score, best_step = cand, s, decision.best_score, step
                accepted = True
                buffer.append({"failures": summary, "rejected": []})
                log(f"[skillopt] step {step}: {decision.action} ({len(edits)} edit(s)) "
                    f"— gate {current_score:.3f}")
            else:
                buffer.append({"failures": summary, "rejected": edits})
                log(f"[skillopt] step {step}: rejected ({len(edits)} edit(s)) — buffered")

        # mechanic #3: epoch-end slow/meta consolidation over the epoch's longitudinal change.
        if accepted and epochs > 1:
            _, _, cur_pass = evaluate(current)
            guidance = sk.slow_update(epoch_start_doc, current,
                                      _longitudinal(tasks, epoch_start_pass, cur_pass), reflection_lm)
            if guidance:
                consolidated = _inject_slow_update(current, guidance)
                h, s, cp = evaluate(consolidated)
                sc = sk.score(h, s)
                if sc >= current_score:
                    current, current_score, c_pass = consolidated, sc, cp
                    if sc > best_score:
                        best, best_soft, best_score = consolidated, s, sc
                    log(f"[skillopt] epoch {epoch + 1}: slow update consolidated (gate {sc:.3f})")
                else:
                    log(f"[skillopt] epoch {epoch + 1}: slow update rejected ({sc:.3f} < {current_score:.3f})")

    if best_score <= seed_gate:
        log(f"[skillopt] best gate {best_score:.3f} does not beat seed {seed_gate:.3f} — keeping the seed.")
        return seed, seed_score, best_soft
    log(f"[skillopt] winner after {step} step(s): gate {best_score:.3f} (seed {seed_gate:.3f})")
    return {**seed, doc_key: best}, seed_score, best_soft
