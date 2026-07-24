# Body-aware Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route approved skills using both descriptions and bounded full bodies while preserving compatibility and serving invariants.

**Architecture:** Keep the existing description matrix for collision checks and add lazily cached, harness-specific content matrices. Rank compatible skills by the maximum of description and content cosine scores, then expose both components in body-free explanations.

**Tech Stack:** Python 3.11+, NumPy, existing embedding backend, pytest

## Global Constraints

- `route_and_load` remains the only serving-selection contract.
- Compatibility filtering happens before ranking.
- At most one selected skill body crosses the boundary.
- Add no dependency and do not change routing thresholds.
- Follow test-driven development: prove red, implement minimal green, rerun focused and full suites.

---

### Task 1: Content representation and cache

**Files:**
- Modify: `mcp_server/router.py`
- Modify: `tests/test_router.py`

**Interfaces:**
- Consumes: `Skill.name`, `Skill.description`, `Skill.body_for(harness)`.
- Produces: `Router._content_matrix(harness: str) -> np.ndarray` and cached description/content embeddings.

- [ ] **Step 1: Write failing cache and representation tests**

Add a fake embedding backend that records documents. Construct a router twice
with the same skills, request a Codex route, and assert the backend embeds
descriptions and content documents only once per distinct text. Assert a body
change reuses the description vector and recomputes only content.

- [ ] **Step 2: Run the focused tests and prove red**

Run:

```bash
python -m pytest -q tests/test_router.py -k "body or content or reuses"
```

Expected: failure because `Router` has no content representation.

- [ ] **Step 3: Implement bounded content documents**

Add:

```python
BODY_CHARS = int(os.environ.get("ROUTER_BODY_CHARS", "16000"))

@staticmethod
def _content_text(skill: Skill, harness: str) -> str:
    return (
        f"Skill: {skill.name}\n"
        f"Description: {skill.description}\n"
        f"Instructions:\n{skill.body_for(harness)[:BODY_CHARS]}"
    )
```

Refactor vector loading into a cache keyed by
`(_MODEL, representation, text)`. Keep `_mat` as the normalized description
matrix and lazily build one normalized content matrix per harness.

- [ ] **Step 4: Run the focused tests and prove green**

Run the command from Step 2. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add mcp_server/router.py tests/test_router.py
git commit -m "Add cached full-body routing documents"
```

### Task 2: Body-aware ranking and explanations

**Files:**
- Modify: `mcp_server/router.py`
- Modify: `tests/test_router.py`
- Modify: `tests/test_routing_eval.py`
- Create: `evals/fixtures/skills/kubernetes-runbook/SKILL.md`
- Create: `evals/fixtures/skills/billing-runbook/SKILL.md`
- Modify: `evals/routing.yaml`

**Interfaces:**
- Consumes: description matrix and `Router._content_matrix(harness)`.
- Produces: ranking tuples carrying skill, aggregate score, description score, content score, and `matched_on`.

- [ ] **Step 1: Write failing route tests**

Use a deterministic fake embedder where two descriptions have equal vectors,
the task matches only the Kubernetes body's vector, and the billing skill sorts
first by name. Assert:

```python
result["match"] == "kubernetes-runbook"
result["matched_on"] == "content"
result["score_components"]["content"] > result["score_components"]["description"]
```

Add a second test marking the body-matching skill incompatible and assert it
cannot win. Add a third test proving Codex and Claude variants can rank
differently.

- [ ] **Step 2: Run the new tests and prove red**

Run:

```bash
python -m pytest -q tests/test_router.py -k "body_aware or variant_content"
```

Expected: the description-only router selects the deterministic tie-breaker.

- [ ] **Step 3: Implement component ranking**

For each compatible skill, compute:

```python
description_score = float(self._mat[index] @ query)
content_score = float(content_mat[index] @ query)
score = max(description_score, content_score)
matched_on = "description" if description_score >= content_score else "content"
```

Sort by aggregate score, priority, then name. Include rounded components and
`matched_on` in direct, related, suggestion, and alternative records. Keep all
alternative records body-free.

- [ ] **Step 4: Add a committed body-disambiguation case**

Create two fixture skills with the same short description and different,
decisive bodies. Add a Kubernetes task to `evals/routing.yaml`. Extend the
committed-suite test to assert at least one selected route reports
`matched_on == "content"`.

- [ ] **Step 5: Run focused routing verification**

Run:

```bash
python -m pytest -q tests/test_router.py tests/test_routing_eval.py tests/test_server.py tests/test_run_task.py
```

Expected: all tests pass.

- [ ] **Step 6: Run the full suite**

Run:

```bash
python -m pytest -q
```

Expected: all tests pass or only explicitly documented environment-dependent
skips.

- [ ] **Step 7: Commit**

```bash
git add mcp_server/router.py tests/test_router.py tests/test_routing_eval.py \
  evals/routing.yaml evals/fixtures/skills/kubernetes-runbook/SKILL.md \
  evals/fixtures/skills/billing-runbook/SKILL.md
git commit -m "Route skills with approved body evidence"
```

### Task 3: Documentation, caller proof, and review

**Files:**
- Modify: `ARCHITECTURE.md`
- Modify: `docs/configuration.md`
- Modify: `SESSION_LOG_2026_07_23.md`

**Interfaces:**
- Consumes: live `route_and_load` response.
- Produces: documented body-aware routing contract and persisted verification evidence.

- [ ] **Step 1: Update operator documentation**

Document `ROUTER_BODY_CHARS`, score components, unchanged threshold policy,
and the requirement for larger held-out calibration before claiming production
lift.

- [ ] **Step 2: Prove the live caller path**

Load the committed fixtures into `STATE`, invoke `route_and_load` with the
Kubernetes task, and assert the selected body, revision, `matched_on`, and score
components. Record the exact command and result in the session log.

- [ ] **Step 3: Run final deterministic verification**

Run:

```bash
git diff --check
python -m pytest -q
```

Expected: clean diff and full suite pass.

- [ ] **Step 4: Request a fresh read-only semantic review**

Review the spec, diff, live caller output, and test output. Fix every BLOCKER
and MAJOR finding, then rerun Step 3.

- [ ] **Step 5: Commit and push**

```bash
git add ARCHITECTURE.md docs/configuration.md SESSION_LOG_2026_07_23.md
git commit -m "Document body-aware routing evidence"
git push -u origin feature/verified-capability-cache
```
