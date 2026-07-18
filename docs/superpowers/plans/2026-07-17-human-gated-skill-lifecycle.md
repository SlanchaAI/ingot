# Human-Gated Skill Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let agents author new skills by default while requiring approval UI action before any new or rewritten skill becomes active.

**Architecture:** `create_skill` validates content and saves a `kind: creation` record in the existing pending queue. A single `approve_pending(skill)` boundary activates new candidates or evidence-backed rewrites; optimizer and canary paths only produce pending recommendations.

**Tech Stack:** Python 3.12, FastMCP 3, FastAPI, pytest, Docker Compose.

## Global Constraints

- Run Python tests inside the `ingot-mcp` Docker image.
- No application-controlled path except `ui.app.approve` may call `approve_pending`.
- Existing rewrite evidence, revision, snapshot, atomic swap, and rollback behavior must remain.
- New-skill activation must be atomic and must preserve pending state on failure.
- Direct operator filesystem edits are outside the application human-gate guarantee.

---

### Task 1: Queue agent-authored creations

**Files:**
- Modify: `tests/test_server.py`
- Modify: `mcp_server/server.py`
- Modify: `.env.example`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: `save_pending(skill: str, data: dict) -> Path`
- Produces: unchanged `create_skill(name: str, description: str, body: str) -> str`, now queue-only

- [x] **Step 1: Replace flag tests with failing queue tests**

Add tests proving `create_skill` creates `runs/pending/<slug>.json`, creates no active directory,
does not reload the router, and rejects a second pending candidate with the same slug.

- [x] **Step 2: Run RED test**

Run: `docker run --rm -v "$PWD:/app" -w /app ingot-mcp python -m pytest tests/test_server.py -q`

Expected: queue tests fail because `create_skill` returns the flag refusal or writes directly to
`skills/`.

- [x] **Step 3: Implement queue-only authoring**

Remove `ENABLE_AGENT_SKILL_WRITES`. After existing slug, duplicate, safety, model, and collision
checks, save this record:

```python
save_pending(slug, {
    "kind": "creation",
    "skill": slug,
    "champion_components": {"description": "", "body": ""},
    "challenger_components": {"description": description, "body": body},
    "changed_components": ["description", "body"],
    "gate": {"promotable": True, "blocked": [], "warnings": []},
    "source": "agent",
})
```

Return `Created candidate '<slug>': awaiting human approval at http://localhost:8080.` Remove the
flag from `.env.example` and Compose.

- [x] **Step 4: Run GREEN tests**

Run: `docker run --rm -v "$PWD:/app" -w /app ingot-mcp python -m pytest tests/test_server.py tests/test_security.py -q`

Expected: all selected tests pass.

---

### Task 2: Activate pending creations only through approval

**Files:**
- Modify: `tests/test_promote.py`
- Modify: `optimize/promote.py`
- Modify: `ui/app.py`
- Modify: `tests/test_ui.py`

**Interfaces:**
- Produces: `approve_pending(skill: str) -> str`
- Removes: production `promote(skill, components=None, evidence=None)` entry point

- [x] **Step 1: Write failing creation-approval tests**

Add tests proving `approve_pending` atomically creates `skills/<slug>/SKILL.md`, preserves
`source: agent`, clears pending only on success, refuses an active-name race, and preserves pending
when writing fails.

- [x] **Step 2: Run RED tests**

Run: `docker run --rm -v "$PWD:/app" -w /app ingot-mcp python -m pytest tests/test_promote.py -q`

Expected: failures because `approve_pending` and the creation branch do not exist.

- [x] **Step 3: Implement the single activation boundary**

Rename existing rewrite promotion to an internal helper. Implement:

```python
def approve_pending(skill: str) -> str:
    pending = load_pending(check_slug(skill))
    if not pending:
        raise ValueError(f"no pending challenger for '{skill}'")
    if pending.get("kind") == "creation":
        return _activate_creation(skill, pending)
    return _activate_rewrite(skill, pending)
```

Creation activation re-runs naming, content, optional model, duplicate, and collision checks; writes
to a temporary sibling directory; atomically renames it to `SKILLS_DIR / skill`; then removes
pending. Rewrite activation retains current evidence validation and atomic swap.

- [x] **Step 4: Route UI approval through `approve_pending`**

Change `ui.app.approve` to call `approve_pending(skill)`. Preserve blocked-gate HTTP 409 behavior.

- [x] **Step 5: Run GREEN tests**

Run: `docker run --rm -v "$PWD:/app" -w /app ingot-mcp python -m pytest tests/test_promote.py tests/test_ui.py -q`

Expected: all selected tests pass.

---

### Task 3: Show pending-only creations in the UI

**Files:**
- Modify: `optimize/promote.py`
- Modify: `ui/app.py`
- Modify: `ui/static/index.html`
- Modify: `tests/test_ui.py`

**Interfaces:**
- Produces: `list_pending() -> list[dict]`
- Extends `/api/skills` entries with `creation: bool`

- [x] **Step 1: Write failing API tests**

Add a pending creation with an empty active library. Assert `/api/skills` returns it with
`pending: true`, `creation: true`, its proposed description, and `has_tasks: false`. Assert
`/api/pending/<slug>` returns `kind: creation` and an addition diff.

- [x] **Step 2: Run RED tests**

Run: `docker run --rm -v "$PWD:/app" -w /app ingot-mcp python -m pytest tests/test_ui.py -q`

Expected: pending-only creation is absent.

- [x] **Step 3: Implement pending union and creation copy**

Add valid pending creations not already present in the active list. Label cards and review detail as
new skills; hide optimize controls until approval. Reuse existing approve/reject buttons.

- [x] **Step 4: Run GREEN tests**

Run: `docker run --rm -v "$PWD:/app" -w /app ingot-mcp python -m pytest tests/test_ui.py -q`

Expected: all UI tests pass.

---

### Task 4: Remove optimizer and canary bypasses

**Files:**
- Modify: `tests/test_optimize.py`
- Modify: `tests/test_canary_visibility.py`
- Modify: `optimize/ab.py`
- Modify: `optimize/canary.py`

**Interfaces:**
- Changes: `run_ab` always saves successful challengers as pending
- Changes: `run_canary` records a `decision: promote` recommendation but never activates

- [x] **Step 1: Write failing bypass tests**

Assert `run_ab` has no `promote_now` parameter. Exercise a canary win with an active skill and
assert the on-disk revision stays unchanged while pending state records the recommendation.

- [x] **Step 2: Run RED tests**

Run: `docker run --rm -v "$PWD:/app" -w /app ingot-mcp python -m pytest tests/test_optimize.py tests/test_canary_visibility.py -q`

Expected: signature assertion fails and auto-promote remains accepted.

- [x] **Step 3: Remove activation controls**

Remove `promote_now`, both `--promote` CLI flags, imports of the activation function, and direct
activation calls. Preserve recommendation/rejection results and pending evidence.

- [x] **Step 4: Run GREEN tests**

Run: `docker run --rm -v "$PWD:/app" -w /app ingot-mcp python -m pytest tests/test_optimize.py tests/test_canary_visibility.py -q`

Expected: all selected tests pass.

---

### Task 5: Reconcile public contract and verify branch

**Files:**
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `agent/run.py`
- Modify: `mcp_server/router.py`
- Modify: `mcp_server/safety.py`
- Modify: `SESSION_LOG_2026_07_17.md`

**Interfaces:**
- Documents: creation-by-default, pending review, and no automated promotion

- [x] **Step 1: Update public language**

Remove flag instructions and every auto-promotion claim. State that `create_skill` queues a
candidate, only approval activates it, canary recommends, and direct filesystem edits remain an
operator-controlled escape hatch.

- [x] **Step 2: Run full verification**

Run:

```bash
docker compose config --quiet
docker compose build
docker run --rm -v "$PWD:/app" -w /app ingot-mcp python -m pytest tests -q
```

Expected: Compose and build exit 0; full suite has no failures.

- [x] **Step 3: Run structural scans**

Run:

```bash
rg -n 'ENABLE_AGENT_SKILL_WRITES|auto-promote|--promote' README.md SECURITY.md .env.example docker-compose.yml agent mcp_server optimize ui tests
rg -n 'approve_pending' optimize ui mcp_server agent
```

Expected: first scan has no live feature references; second scan shows one production caller in
`ui/app.py` plus its definition and tests.

- [ ] **Step 4: Run GitNexus and CodeScene gates**

Run GitNexus change detection, CodeScene `pre_commit_code_health_safeguard`, and CodeScene
`analyze_change_set` against `master`.

Expected: acknowledged impact only; both CodeScene quality gates pass.

- [ ] **Step 5: Commit and push PR correction**

Commit in reviewable units with the required co-author trailer, push
`launch/human-gated-skills`, and confirm PR #18 CI passes. Do not merge.
