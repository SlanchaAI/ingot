# Skills-Only Scope Correction Implementation Plan

> **Superseded in part.** This is the dated record of a 2026-07-15 plan and is kept as written,
> including the two unchecked boxes in Task 4: they record where that PR's process stopped, not open
> work. Two things it describes have since changed. `GEPA` in the tech stack means the sequential
> body loop, which was removed: the body pass is now the parallel best-of-N search in
> `optimize/bestofn.py`, and GEPA remains only as the description pass's reflection step.
> `create_skill`, listed as preserved, survives by name but no longer activates anything: it writes
> a quarantined pending record that a human approves in the UI, per
> [the 2026-07-17 human-gated skill lifecycle plan](2026-07-17-human-gated-skill-lifecycle.md).
> The additive `route_and_load` scope this plan argues for is what shipped. See
> [ARCHITECTURE.md](../../../ARCHITECTURE.md) for the current design.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shrink PR #2 to additive MCP routing and safe skill improvement while preserving the existing Docker product.

**Architecture:** Restore `master` packaging, Docker, demo-agent, fetch, and README surfaces. Add `route_and_load` beside,not instead of,the existing MCP tools. Shared roots follow the local writable skill root. Retain revisioned Behavioral CI and promotion hardening.

**Tech Stack:** Python 3.12 Docker image, FastMCP 3 HTTP transport, fastembed, LangGraph, Langfuse, GEPA, pytest.

## Global Constraints

- No packaged CLI or console entrypoint.
- No harness adapter bundle in this PR.
- No references or code for separate products.
- Existing Docker quick start and five MCP tools remain compatible.
- Tests precede behavioral changes.

---

### Task 1: Restore additive MCP compatibility

**Files:**
- Modify: `tests/test_server.py`
- Modify: `mcp_server/server.py`
- Modify: `mcp_server/registry.py`
- Modify: `tests/test_registry.py`

**Interfaces:**
- Preserves: `list_skills`, `suggest_skills`, `get_skill`, `create_skill`, `reload_skills`
- Adds: `route_and_load(task, harness, cwd, available_tools=None, available_mcps=None) -> dict`

- [x] Write failing registration and external-root/local-authoring coexistence tests.
- [x] Run targeted tests and confirm expected failures.
- [x] Restore the five tools and add `route_and_load` as the sixth.
- [x] Keep the local writable root first and append configured roots.
- [x] Run targeted tests.

### Task 2: Remove packaged CLI and restore launch workflow

**Files:**
- Delete: `mcp_server/cli.py`
- Delete: `tests/test_cli.py`
- Delete: `pyproject.toml`
- Restore from `master`: `Dockerfile`, `docker-compose.yml`, `.env.example`, `requirements.txt`, `scripts/fetch_skills.sh`, `requirements-guard.txt`
- Delete: `skills.lock.json`
- Delete: `adapters/`
- Restore from `master`: `agent/run.py`

**Interfaces:**
- Preserves: `python -m mcp_server.server`, `docker compose up --build`
- Removes: the packaged console-command surface introduced by the draft PR

- [x] Remove CLI-only tests and implementation.
- [x] Restore Docker, dependency, demo-agent, and fetch files mechanically from `master`.
- [x] Restore module startup in `mcp_server.server`.
- [x] Run server, agent, and Compose tests.

### Task 3: Restore product documentation

**Files:**
- Restore and modify: `README.md`
- Delete: `ASSUMPTIONS.md`
- Rewrite: `SESSION_LOG_2026_07_14.md`, `SESSION_LOG_2026_07_15.md`
- Delete: `docs/superpowers/specs/2026-07-14-core-skill-router-design.md`
- Delete: `docs/superpowers/plans/2026-07-14-route-improve-loop.md`

**Interfaces:**
- Documents: existing Docker quick start, six MCP tools, optional shared roots, stronger promotion evidence.

- [x] Restore README from `master`.
- [x] Add concise additive routing and Behavioral CI sections.
- [x] Remove stale architecture artifacts and rewrite session logs.
- [x] Scan every tracked file for forbidden product names and packaged CLI commands.

### Task 4: Verify and publish correction

**Files:**
- Modify only files required by failed regression checks.

**Interfaces:**
- Produces: corrected PR #2 against `master`.

- [x] Run full pytest suite.
- [x] Run `docker compose config --quiet`.
- [x] Run GitNexus change detection against `origin/master`.
- [ ] Commit and push the correction.
- [ ] Replace PR #2 title/body with the skills-only scope and verification evidence.
