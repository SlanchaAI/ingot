# Session log — 2026-07-15

## Goal

Prepare Skill Router for open-source developer adoption while preserving its existing skills-only
MCP/Docker product and self-improvement loop.

## Scope correction

An initial draft pull request accidentally introduced a packaged CLI, harness adapters, and product
positioning outside this repository's established boundary. Operator review corrected that direction.

Final boundary:

- Keep the existing Docker quick start and Python module entrypoints.
- Keep all five existing discovery, loading, authoring, and reload MCP tools.
- Add `route_and_load` as an optional sixth tool, not a replacement workflow.
- Keep skill improvement and behavioral promotion checks inside the existing optimizer/UI loop.
- Do not add a packaged console command, PyPI launch story, or harness plugin bundle in this PR.
- Do not integrate or position unrelated products through this repository.

The separate UI integration pull request was closed. Its branch remains preserved outside the
Skill Router merge path.

## Review findings retained

- Missing holdout data is marked leaked and cannot produce promotable evidence.
- Recall@3, no-route precision, routing regressions, and compatible-harness parity are computed when
  a routing description changes.
- Project path patterns inspect real files below the working directory.
- Complete logical skill revisions include bundled references and scripts.
- Bundled drift, traversal, external symlink reads, and `file:SKILL.md` overwrite attempts fail
  closed before promotion.
- Prior revisions are snapshotted; promotion stages the challenger and rolls back a failed swap.

## Verification

- Full suite: 155 passed, 1 skipped.
- Docker Compose configuration parses with the original local `.env` contract.
- `python -m mcp_server.server` serves HTTP successfully.
- Live MCP discovery returns the existing five tools plus additive `route_and_load`.
- No packaged CLI module, console entrypoint, package manifest, or harness adapter remains.
- Repository-wide scope scan contains no names belonging to the separate product.
- GitNexus maps the retained diff to 19 routing, authoring, and promotion flows; risk is critical by
  breadth, with those paths covered by regression tests.

## Artifact catalog

- `docs/superpowers/specs/2026-07-15-skills-only-scope-design.md` — corrected repository boundary.
- `docs/superpowers/plans/2026-07-15-skills-only-scope.md` — correction implementation plan.
- `mcp_server/routing_eval.py` — reusable routing metrics for promotion checks.
- `evals/routing.yaml` — deterministic routing regression cases.
- `optimize/evidence.py` — revisioned Behavioral CI evidence artifact.
