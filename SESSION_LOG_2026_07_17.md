# Session log: 2026-07-17

## Objective

Make agent-authored skill creation useful by default while making human approval the only
application path that can activate a new or rewritten skill.

## Changes

- Changed `create_skill` from a live filesystem write to a validated `kind: creation` candidate in
  `runs/pending/`.
- Added `approve_pending` as the single activation boundary. Only the approval UI calls it in
  production.
- Made new-skill activation atomic and revalidated names, content, model guard results, duplicate
  descriptions, and filesystem collisions at approval time.
- Preserved pending state on activation failure and retained the rewrite path's evidence checks,
  snapshots, atomic swap, and rollback.
- Added pending-only creations to the approval UI with an addition diff, explicit
  `Approve & activate` action, and no optimize action before activation.
- Removed optimizer and canary activation flags and calls. Both paths now stop at pending
  recommendations.
- Updated public docs, agent prompt, Compose configuration, and safety comments to match the
  human-gated lifecycle.

## Decisions

- Agent skill creation stays enabled because it is the product's core loop; the trust boundary sits
  at activation, not authorship.
- UI approval is the sole normal application caller. Direct operator edits under `skills/` remain
  an explicit filesystem escape hatch outside this guarantee.
- Canary checkpoint wins must contain complete challenger evidence so later approval never depends
  on an automated activation shortcut.
- Kept PR #18 as a draft for James's review. No merge performed.

## Verification

- Docker Compose configuration: passed.
- Docker image build: passed.
- Merged current `origin/master` into the branch and preserved its parallel optimizer and unified
  skill-list changes while resolving three launch-path conflicts.
- Full Docker suite after merge: `299 passed, 1 skipped, 1 warning in 16.84s`.
- Affected UI Playwright review: pending creation card, diff, and approval action rendered; all
  affected API requests returned 200; zero console errors.
- `git diff --check`: passed.
- Structural scan: one production `approve_pending` caller (`ui/app.py`); no production generic
  `promote` function or optimizer/canary activation flag.
- CodeScene reviews: `optimize/promote.py` 10.0; `ui/app.py` 10.0.
- CodeScene pre-commit safeguard: passed with no regressions.
- CodeScene branch change-set analysis against current `origin/master`: passed with no regressions.
- GitNexus change detection: CRITICAL central-flow impact, expected and acknowledged before this
  implementation.
- Draft PR #18: mergeable; GitHub CI passed.

## Artifacts

- `docs/superpowers/specs/2026-07-17-human-gated-skill-lifecycle-design.md`: approved lifecycle
  and trust-boundary design.
- `docs/superpowers/plans/2026-07-17-human-gated-skill-lifecycle.md`: implementation and
  verification checklist.
- `SECURITY.md`: public activation boundary and operator escape-hatch contract.

## Remaining work

1. Let James review draft PR #18.
2. Merge only on explicit request.
