# Component-pass optimization: one objective per component role

Status: DRAFT for review — no further optimization runs until this is agreed.

## Problem

A skill has three kinds of text component, each with a different production role:

| component | production role | who consumes it |
|---|---|---|
| `description` | routing trigger | the embedding router (cosine match against the task) |
| `body` | instructions | the serving agent (via `get_skill` / `route_and_load`) |
| `file:<path>` | reference / tooling | nothing in this stack today (other harnesses may read them from disk) |

GEPA's inner loop scores every candidate with one metric: an LLM judge grading a bare-rollout
*answer*. In that bare rollout the description and body are indistinguishable (both are pasted
into the prompt), and files are invisible to production but visible to the rollout. Result,
observed across six runs on 2026-07-15:

- GEPA repeatedly moved behavioral rules ("always output complete runnable code") into the
  `description` — winning the inner loop, then either **blocked by the routing gate**
  (recall@3 1.0 → 0.5 when the description bloated 437 → 1587 chars) or **losing the A/B**
  (the serving agent never reads the description as instructions; the same improved body under
  the original description scored 0.0).
- When judge/provider variance scored the seed high (≥ ~0.85 on the 4-task train set), GEPA
  found "no better candidate" — the failure the outer A/B measures (agent writes code to its
  scratch filesystem and describes it) does not reproduce in a bare rollout at all.

One shared metric grades three roles; it can only measure one of them.

## Design: one pass per component, each scored by its role's own metric

### Pass 1 — body (IMPLEMENTED, the default)

- Mutable: `body` only (`OPTIMIZE_COMPONENTS=body`). The frozen `description` still renders into
  rollouts for serving fidelity but cannot be mutated.
- Objective: LLM judge on train tasks (existing), length penalty, deletion steering.
- Gate: full-agent A/B on holdout + margin/samples/regression checks + `RETENTION_WARN`.
- Cost: ~$1 / ~30–40 min per run (budget 60).

### Pass 2 — description (IMPLEMENTED: `optimize <skill> --description`)

- Mutable: `description` only.
- Objective: **the routing suite, not the quality judge** — score a candidate description by
  re-embedding it and computing recall@3 / top-1 / no-route precision over the `routing:` cases
  in the skill's task YAML, plus collision margin against every other skill's description and
  cross-harness parity. All computed by the local CPU embedding router: **no LLM rollouts**.
  GEPA's only model calls are its handful of reflections (~$0.02/run, seconds per candidate).
- Gate: routing metrics strictly ≥ champion on every axis, collision < `COLLISION_SCORE`,
  human approval in the UI as usual. Quality A/B unnecessary by construction (the serving agent's
  instructions are unchanged).
- Prerequisite: a `routing:` case set for the skill (exists for `pdf`; auto-drafting routing
  cases for skills that lack them is a small extension of the existing task drafter).

### Pass 3 — bundled files (BLOCKED on a real measurement)

- Sequencing files into their own pass does not help: nothing measures them. The A/B doesn't
  serve them, scripts never execute, and a files-only rollout judge has the same blindness that
  broke the description.
- Prerequisites, per file kind:
  - `scripts/*`: execution-grounded eval — SHIPPED 2026-07-16 as per-task `check:` specs
    (fixture + assert, run in a scratch dir inside the disposable optimize container; broken
    fixtures are inconclusive, never held against the answer). Remaining gap: file-serving
    rollouts so a rewritten script is exercised the way a consuming harness would use it.
  - docs (`reference.md`, `forms.md`): rollouts shaped like a file-reading harness (body
    instructs "read FORMS.md", rollout actually inlines it) so the judge can attribute quality
    to the file content.
- Until then: files are optimizable only by explicit opt-in (`OPTIMIZE_COMPONENTS=body,file:X`),
  are diffed for human review, and the docs warn against including scripts.

## Orchestration

Passes are separate invocations, run in order, each with its own pending/approval cycle:

```
optimize pdf                 # pass 1: body (today's command, unchanged)
optimize pdf --routing       # pass 2: description (proposed flag)
```

The continuous loop (`optimize-loop`) keeps running pass 1 only; pass 2 joins it once
auto-drafted routing cases exist. No interleaving within a single GEPA run — the alternation
GEPA does internally is exactly what let quality pressure leak into the description.

## Non-goals

- No simultaneous multi-component GEPA by default (the root cause of this document).
- No Langfuse-managed judge for pass 2 (routing metrics are local and free).
- No files pass at launch.

## Decisions (resolved 2026-07-15)

1. Flag spelling: component flags on the one `optimize` command — `--body` (default),
   `--description`, `--scripts` (friendly refusal until execution-grounded evals exist).
2. Pass-2 gate: no regression on any routing metric + at least one strict improvement +
   collision check + human approval. No margin requirement — the pass is nearly free to re-run.
3. Routing cases are required, not auto-drafted (friendly error names the file to edit);
   auto-drafting them is a later extension of the task drafter.
