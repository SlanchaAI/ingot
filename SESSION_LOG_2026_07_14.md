# Session log — 2026-07-14

## Goal

Prepare Skill Router for Friday open-source developer adoption without abandoning its useful
self-improvement loop or Behavioral CI.

## Product decision

Positioning remains: **Route to the right skill. Learn from failures. Improve it safely.**

Behavioral CI is the trust layer inside improvement, not a benchmark-only product. Flow:

```text
route + load → observe exact revision → improve challenger → Behavioral CI → approve → promote
```

CARN remains the enterprise consumer for fleet trajectory analysis, stuck detection, rescue paths,
governance, and dashboards. OSS emits the portable evidence contract.

## Market/persona findings

- Native Agent Skills already provide progressive disclosure.
- Vercel/GitHub/agentskill registries make installer and marketplace positioning weak.
- Anthropic skill-creator and Microsoft SkillOpt make generic optimization positioning crowded.
- Strong combined wedge: one shared cross-harness runtime plus revisioned, behavior-gated continuous
  improvement.

## Implemented

- External configurable roots with deterministic first-root precedence and duplicate warnings.
- SHA-256 skill revisions and harness-specific bodies.
- Harness/platform/scope/tool/MCP/trust/activation filtering before ranking.
- One-call `route_and_load`; default MCP has no mutation or catalog tools.
- Local CLI: `index`, `route`, `serve`, `doctor`, `eval`, `improve`, `review`, `promote`.
- Stdio default; HTTP explicit and loopback-only.
- Validated tiny Claude and Codex adapters.
- Full-agent champion/challenger flow now evaluates both routing description and skill body.
- Portable `evidence.json` and `EVIDENCE.md` with revision attribution, per-case deltas, scrubbed
  trajectory shape, and first behavioral divergence.
- Promotion requires passing evidence, matching live/draft revisions, snapshot, and a staged swap
  with rollback.
- Server automatically hot-refreshes after external revision changes.
- Traversal/symlink write protection.
- Mutable third-party fetch disabled pending reviewed lock entries.

## Known release blockers/follow-ups

- Confirm copyright ownership/attribution with James Maki and SlanchaAI before public release.
- Run current 102-skill recall/no-route/latency/token-overhead proof; do not publish estimates as
  measured results.
- Optional optimizer still defaults to OpenRouter-compatible execution. Build harness-native OAuth
  execution path; API keys must remain opt-in.
- Populate `skills.lock.json` only after pin/checksum/license review.

## Artifact catalog

- `docs/superpowers/specs/2026-07-14-core-skill-router-design.md` — approved architecture and product boundary.
- `docs/superpowers/plans/2026-07-14-route-improve-loop.md` — executable implementation plan.
- `ASSUMPTIONS.md` — claims, confidence, and unresolved items.
- `adapters/` — tiny Claude and Codex bootstrap integrations.
- `optimize/evidence.py` — portable Behavioral Skill CI artifact contract.
- `evals/routing.yaml` — public routing fixture shape.
- `skills.lock.json` — fail-closed third-party source lock schema.
