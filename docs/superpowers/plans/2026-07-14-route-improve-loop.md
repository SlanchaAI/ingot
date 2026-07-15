# Route and Improve Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a local-first Skill Router that loads one compatible skill, improves exact revisions from observed failures, and requires Behavioral CI plus human approval before promotion.

**Architecture:** Keep Agent Skills as canonical files in external roots. Build one immutable in-memory registry shared by CLI and a one-tool MCP server. Preserve the existing optimizer, but make it emit a revisioned evidence contract and route through `route_and_load`; promotion becomes atomic and gate-enforced. Claude and Codex adapters contain only bootstrap instructions and stdio configuration.

**Tech Stack:** Python 3.10+, FastMCP 3, fastembed, NumPy, PyYAML, argparse, pytest; optional LangGraph/Langfuse/GEPA/FastAPI extras.

## Global Constraints

- Product hierarchy: route → learn → improve → Behavioral CI gate → approve/promote.
- `route`, `index`, and stdio serving require no API key, Docker, hosted tracing, or network after model installation.
- Default MCP exposes only read-only `route_and_load`.
- No live skill mutation without a passed gate and explicit promotion.
- Agent Skills compatibility remains the base format; router metadata is namespaced.
- CARN consumes OSS evidence but remains a separate enterprise implementation.
- Existing optimizer and UI behavior remain available through optional dependencies.

---

### Task 1: External revisioned registry

**Files:**
- Modify: `mcp_server/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Produces: `configured_roots(explicit=None) -> list[Path]`
- Produces: `load_skills(skills_dir=None, roots=None) -> list[Skill]`
- Produces: `Skill.revision`, `Skill.root`, `Skill.metadata`, `Skill.body_for(harness)`

- [ ] Write failing tests for path-separated roots, duplicate identities, revision changes, namespaced metadata defaults, and contained harness variants.
- [ ] Run `pytest tests/test_registry.py -q`; verify new tests fail because interfaces are missing.
- [ ] Implement root resolution, canonical containment, duplicate rejection, SHA-256 revisions, metadata parsing, and variant loading.
- [ ] Run `pytest tests/test_registry.py -q`; verify pass.
- [ ] Commit `feat: index external revisioned skill roots`.

### Task 2: Filtered one-call routing and local CLI

**Files:**
- Modify: `mcp_server/router.py`
- Rewrite: `mcp_server/server.py`
- Create: `mcp_server/cli.py`
- Create: `pyproject.toml`
- Test: `tests/test_router.py`
- Create: `tests/test_cli.py`
- Create: `tests/test_server.py`

**Interfaces:**
- Produces: `Router.route(task, harness, cwd, available_tools=(), available_mcps=(), platform=None) -> dict`
- Produces response keys: `match`, `score`, `reason`, `skill_body`, `skill_root`, `revision`, `alternatives`
- Produces CLI commands: `index`, `route`, `serve`, `doctor`

- [ ] Write failing tests for no-route, harness/platform/scope/tool/MCP/trust/manual filters, priority ordering, response schema, and CLI JSON parity.
- [ ] Run targeted tests; verify failures are missing behavior.
- [ ] Implement filtering before embedding rank and deterministic response construction.
- [ ] Register only `route_and_load` on the default FastMCP server; stdio default, explicit loopback HTTP.
- [ ] Implement argparse CLI and lean `pyproject.toml` dependency extras.
- [ ] Run targeted tests and full suite; verify pass.
- [ ] Commit `feat: add read-only route and load runtime`.

### Task 3: Thin Claude and Codex adapters

**Files:**
- Create: `adapters/claude/skill-router/SKILL.md`
- Create: `adapters/claude/mcp.json`
- Create: `adapters/codex/.codex-plugin/plugin.json`
- Create: `adapters/codex/skills/skill-router/SKILL.md`
- Create: `adapters/codex/.mcp.json`
- Create: `tests/test_adapters.py`

**Interfaces:**
- Consumes: `skill-router serve --stdio`
- Produces: identical bootstrap policy, differing only in fixed `harness` argument.

- [ ] Write failing structural tests that parse manifests, enforce one bootstrap skill, reject catalog dumps, and compare policy text.
- [ ] Run `pytest tests/test_adapters.py -q`; verify missing files fail.
- [ ] Add minimal adapters and installation notes without modifying user harness configuration.
- [ ] Run adapter tests; verify pass.
- [ ] Commit `feat: add thin Claude and Codex adapters`.

### Task 4: Revisioned Behavioral CI evidence

**Files:**
- Create: `optimize/evidence.py`
- Modify: `optimize/ab.py`
- Modify: `agent/run.py`
- Modify: `tests/test_run_task.py`
- Create: `tests/test_evidence.py`
- Modify: `tests/test_optimize.py`

**Interfaces:**
- Produces: `build_evidence(summary, champion_revision, challenger_revision) -> dict`
- Produces: `write_evidence(evidence, root) -> tuple[Path, Path]` for `evidence.json` and `EVIDENCE.md`
- Changes agent routing instruction/tool parsing from two calls to `route_and_load`.

- [ ] Write failing tests for exact revision attribution, per-case score deltas, gate reasons, token deltas, optional first-divergence records, deterministic Markdown, and routed skill extraction.
- [ ] Run targeted tests; verify expected failures.
- [ ] Implement portable evidence schema and report renderer.
- [ ] Update A/B variants to override `route_and_load`, retain full-agent execution, and write evidence for every challenger including blocked ones.
- [ ] Run targeted and full tests; verify pass.
- [ ] Commit `feat: emit behavioral skill CI evidence`.

### Task 5: Safe improvement lifecycle

**Files:**
- Modify: `optimize/promote.py`
- Modify: `mcp_server/registry.py`
- Modify: `ui/app.py`
- Modify: `tests/test_security.py`
- Create: `tests/test_promote.py`

**Interfaces:**
- Produces: quarantined pending revisions under `runs/pending/`.
- Produces: immutable backups under `runs/revisions/<skill>/`.
- Promotion requires `gate.promotable is True` from matching evidence.

- [ ] Write failing tests for traversal/symlink containment, atomic replace, immutable previous revision, gate refusal, and rollback data.
- [ ] Run targeted tests; verify failures.
- [ ] Implement contained atomic writes and revision snapshots; remove default MCP authoring and reload surfaces.
- [ ] Make UI refuse blocked promotion.
- [ ] Run security and full suites; verify pass.
- [ ] Commit `security: gate and revision skill promotion`.

### Task 6: Launch documentation and proof

**Files:**
- Rewrite: `README.md`
- Modify: `.env.example`
- Modify: `docker-compose.yml`
- Modify: `Dockerfile`
- Create: `evals/routing.yaml`
- Create: `SESSION_LOG_2026_07_14.md`

**Interfaces:**
- Documents zero-key route path and optional improvement path.
- Docker demo explicitly opts into loopback HTTP while local CLI defaults to stdio.

- [x] Add a deterministic routing fixture and run it through the CLI.
- [x] Rewrite first-run and product story around route-and-improve, with Behavioral CI as promotion gate.
- [x] Document dependency extras, adapter installation, security model, CARN boundary, and unresolved copyright ownership.
- [x] Build wheel, install into a clean venv, run `index`, `route`, and stdio smoke checks.
- [x] Run full tests, `pip check`, package metadata validation, and staged-scope review.
- [ ] Commit `docs: prepare route and improve launch`.
- [ ] Push `launch/core-skill-router` and open a draft PR against `master`.

## Self-review

- Spec coverage: runtime routing, improvement, Behavioral CI, approval, revision attribution, adapters, safe defaults, packaging, and CARN boundary all map to tasks.
- Deferred from Friday implementation: hosted auth, marketplace, automated harness mutation, third-party source lock updater, and CARN implementation. README must label these correctly rather than implying completion.
- Type consistency: `Skill.revision` feeds route responses and Behavioral CI; `route_and_load` is shared by MCP, CLI, demo agent, and optimizer variants; promotion consumes `gate.promotable` from the same evidence.
- Placeholder scan: no TBD/TODO implementation steps.
