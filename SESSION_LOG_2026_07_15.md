# Session log — 2026-07-15

## Goal

Close adversarial review findings, prove the install surface from a built wheel, and publish the
Friday launch work as a draft pull request.

## Spruce-comet review

Wire identity was re-keyed to v3.2 as `briar-heath@wireup.net`; the verified peer channel now accepts
signed events. Spruce-comet reviewed commit `5947fa5` and found three launch-blocking trust gaps:

1. Missing holdout data silently reused training tasks.
2. Recall@3 and no-route precision were named but not computed or gated.
3. Project path patterns matched the cwd string rather than files below cwd.

Commit `5b42e9b` fixes the mechanisms, not only fixtures: absent holdout evidence is marked leaked and
cannot promote; the committed routing suite computes and gates both metrics plus cross-harness
parity; path scope inspects real project files. It also closes review findings in `doctor`, root
precedence, router metadata validation, and the optimizer's dead MCP dependency. Re-review requested
over Wire; result must be checked before PR handoff.

Final self-review found a separate revision-binding gap: the live revision covered `SKILL.md` and
harness variants but not bundled references/scripts. The complete logical skill is now hashed;
prospective challenger evidence uses that same hash, bundled drift blocks promotion, external
symlink reads fail closed, and `file:SKILL.md` cannot bypass structured serialization.

## Packaging decisions

- Keep one distribution for the first run; dependency extras preserve runtime boundaries.
- Base wheel includes core routing, CLI, and stdio MCP only.
- Optimizer, approval UI, semantic guard, and development tools remain optional.
- Wheel must include optimizer task YAML and UI static assets.
- Docker is an optional demo surface and explicitly opts into loopback HTTP.
- Mutable third-party fetch remains disabled until sources are commit-pinned with hashes, provenance,
  and licenses.

## Verification evidence

- Full suite: 165 passed, 1 skipped.
- Built sdist and wheel; Twine accepted both metadata records.
- Clean wheel install indexed 11 fixture skills and routed through the installed console script.
- Routing suite: 15 cases, top-1 1.0, Recall@3 1.0, no-route precision 1.0.
- Clean-home doctor: no issues; only `route_and_load` is model-facing; stdio is default.
- Installed dependency check: no broken requirements.
- Stdio server remained live through the process smoke window.
- Claude skills and Codex skill/plugin passed their official validators.
- Docker Compose configuration parsed successfully.
- Warm 102-entry local latency: median 4.892 ms, p95 5.13 ms, maximum 5.663 ms across 250 routes
  after 10 warmups. Entries were synthesized from 34 real local skill descriptions to isolate
  102-item retrieval cost; repeat with the final public catalog before publishing the number.

## Artifact catalog

- `evals/routing.yaml` — deterministic routing, no-route, filter, priority, conflict, and parity cases.
- `evals/fixtures/` — committed skill and real-project fixtures for the routing gate.
- `optimize/evidence.py` — revisioned Behavioral CI evidence contract.
- `skills.lock.json` — empty, fail-closed third-party source manifest.
- `adapters/` — thin Claude and Codex bootstrap integrations.
