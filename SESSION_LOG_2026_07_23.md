# Session log — 2026-07-23

## Goal

Map the Ingot × CARN competitive field, identify defensible mechanisms, and
implement the smallest measured, live-path capability-cache slices.

## Why

Ingot already governs versioned Agent Skills. CARN already explores
deterministic replay. The missing product boundary is a safe route from fuzzy
capability discovery to exact, validated execution reuse without weakening
Ingot's human evidence gate.

## Decisions

- Ingot remains the activation and governance plane; CARN remains the
  execution record/replay/compiler plane.
- `route_and_load` remains the sole serving-selection contract.
- Body-aware retrieval is the first implementation slice because the strongest
  routing evidence shows full skill bodies materially affect selection.
- Semantic similarity may nominate replay candidates but never authorize
  side effects. Exact compatibility fingerprints and node validators are
  required.
- AGPL-3.0 and unlicensed sources are idea-only. Implementation is clean-room.
- Defensibility comes from permissioned compatibility and outcome evidence,
  not from cache mechanics.

## Artifacts

- `docs/ingot-carn_ATLAS.md` — competitor map, architecture decision, take
  ledger, moat, kill conditions, and follow-through tracker.
- `analysis/ingot-carn_index.jsonl` — normalized discovery index.
- `analysis/ingot-carn_dossiers.jsonl` — deep-read target dossiers.
- `docs/superpowers/specs/2026-07-23-body-aware-routing-design.md` — first
  implementation-slice contract.
- `docs/superpowers/plans/2026-07-23-body-aware-routing.md` — test-driven
  implementation and caller-proof plan.
- `mcp_server/router.py` — cached body-aware ranking with component
  explanations and harness-specific content.
- `evals/fixtures/skills/{billing-runbook,kubernetes-runbook}/` — committed
  description-tie fixture whose decisive evidence lives in the body.

## Verification

Initial focused test attempt:

```text
python -m pytest -q tests/test_router.py tests/test_routing_eval.py
```

Collection succeeded, but the local interpreter lacks `onnxruntime`; 16 tests
failed and 4 errored before router execution. This is an environment baseline,
not a product regression. Use the repository's container or install the pinned
runtime before claiming deterministic green.

The repository's existing isolated runtime at
`.worktrees/behavioral-skill-ci/.venv` includes the pinned ONNX Runtime and was
used for implementation verification.

Red test:

```text
python -m pytest -q tests/test_router.py -k "body_aware or variant_content or body_change"
3 failed, 1 passed
```

The description-only router selected `billing-runbook` by its alphabetic
tie-breaker and never embedded content.

Green focused verification:

```text
python -m pytest -q tests/test_router.py tests/test_routing_eval.py tests/test_server.py tests/test_run_task.py
48 passed in 37.23s
```

The committed routing suite also routes the description-tied Kubernetes case
on `matched_on=content` with the real pinned Qwen embedding backend.

Live MCP caller probe:

```text
STATE.reload([evals/fixtures/skills])
route_and_load("Diagnose a Kubernetes pod stuck in CrashLoopBackOff.", "codex", cwd)
match=kubernetes-runbook
matched_on=content
description=0.293
content=0.760
revision=0020c4b6dbb84fd682f25a0e7af8cef70e9b93c7e8455c8d1c9399f36191a962
```

The first live probe used a 16,000-character body projection and failed inside
ONNX Runtime because the decoder attention tile would exceed 4 GB. A
4,000-character projection passed but took roughly 47 seconds to build cold.
The final 1,000-character default preserved the body-only match, built the cold
content index in 10.502 seconds, and averaged 51.82 ms per hot route on this
machine. Values above 4,000 now fail closed at router construction.

Final verification used the complete dependency environment:

```text
.worktrees/ingot-launch-teaser/.venv/bin/python -m pytest -q
532 passed, 3 skipped, 1 warning in 53.03s
```

The warning is Starlette's existing `httpx` deprecation notice.

An external AI-council run was not submitted: the healthy roster included a
metered API member and this task had no API-spend opt-in. Three read-only
research agents supplied independent routing, replay, and cache critiques
instead. Per the white-space method, the commercial verdict remains REVISE
until buyer and aggregation-rights validation passes.
