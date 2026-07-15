# Skill Router

**Route to the right skill. Learn from failures. Improve it safely.**

Skill Router is a local control plane for shared [Agent Skills](https://agentskills.io). It keeps the
full library outside Claude and Codex native discovery, selects one compatible skill for each
nontrivial task, and returns that skill body through one read-only tool call.

When a skill underperforms, the improvement loop mines its failures, proposes a challenger, runs the
champion and challenger through the full agent, and quarantines the result. Behavioral Skill CI is
the promotion gate: it proves routing and execution improved before a human can make the revision
live.

```text
task → route + load → observed outcome → improve → Behavioral CI → approve → promote
```

## Why this exists

Native Agent Skills already support progressive disclosure. Large multi-harness libraries still pay
for catalog metadata, duplicate installation state, and different selection behavior in each
harness. Skill Router adds one shared runtime decision and one shared improvement history without
building another marketplace.

- One external library for Claude and Codex.
- One model-facing tool, `route_and_load`.
- One selected body—or no skill. Never a catalog dump.
- Local embeddings; warm routes require no network.
- Revision attribution for every routed skill and improvement result.
- Full-agent champion/challenger evaluation before promotion.
- Human approval and staged, reversible promotion.

## Install and route

Python 3.10 or newer:

```bash
pip install skill-router

skill-router index ~/.agents/skills
skill-router route "debug this failing post-tool hook"
skill-router serve --stdio
```

For a source checkout:

```bash
python -m pip install -e .
skill-router index ~/.agents/skills
```

`index` validates and remembers one or more roots. `route` uses the saved roots unless `--root` is
provided. The first embedding-model installation may download model files; routing is local after
that.

Configure several roots directly when needed:

```bash
export SKILL_ROUTER_PATHS="$HOME/Source/shared-skills:$HOME/.agents/skills"
skill-router doctor
```

Roots have declared precedence. When two roots contain the same skill name, the first root wins and
`index` emits a visible warning; duplicates never shadow silently.

## One-call runtime

The default MCP server exposes only:

```text
route_and_load(task, harness, cwd, available_tools=[], available_mcps=[])
```

Response:

```json
{
  "match": "systematic-debugging",
  "score": 0.812,
  "reason": "compatible codex skill; cosine 0.812",
  "skill_body": "...selected instructions...",
  "skill_root": "/Users/me/.agents/skills/systematic-debugging",
  "revision": "c8a2...",
  "alternatives": [
    {"name": "debugging-hooks", "score": 0.701, "reason": "compatible alternative; cosine 0.701"}
  ]
}
```

Below threshold, `match` is `null` and `skill_body` is empty. Alternatives never contain bodies.
There is no model-facing list, author, reload, or promotion tool.

The server defaults to stdio:

```bash
skill-router serve --stdio
```

HTTP is explicit and loopback-only:

```bash
skill-router serve --http --host 127.0.0.1 --port 8000
```

## Thin harness adapters

Reference adapters live under [`adapters/`](adapters/):

- Claude: one 66-word bootstrap skill plus stdio MCP configuration.
- Codex: one validated plugin with the same bootstrap policy and stdio server.

The full library must not also remain installed in the harness-native skill directory. Leave only
the bootstrap there; keep shared skills in indexed external roots. The adapters never delete or move
existing files automatically.

## Routing metadata

Ordinary Agent Skills work without changes. Optional router fields live under a namespaced metadata
block:

```yaml
---
name: deploy-service
description: Use when deploying or rolling back a service in a managed environment.
metadata:
  skill-router:
    harnesses: [claude, codex]
    scopes: [project]
    path_patterns: ["*/service-repo/*"]
    required_tools: [bash]
    required_mcps: []
    trust: reviewed
    activation: automatic
    platforms: [macos, linux]
    priority: 60
    conflicts: []
---
```

Filtering happens before embedding rank. Manual or blocked skills are never automatically returned.
Harness variants may live at `variants/claude.md` and `variants/codex.md`; missing variants fall back
to the main `SKILL.md` body.

## Route and improve

Install improvement dependencies:

```bash
pip install 'skill-router[optimizer]'
```

Then run the useful loop:

```bash
skill-router improve pdf
skill-router review pdf
skill-router promote pdf
```

`improve` retains the project’s GEPA-based optimization path:

1. Load failures and a held-out task set for the exact current skill revision.
2. Propose targeted description/body changes.
3. Run champion and challenger through the full agent.
4. Check judge score, objective execution checks, token cost, catastrophic regressions, routing
   collisions, and held-out routing behavior.
5. Write a quarantined challenger plus evidence.

The current optional optimizer backend uses an OpenAI-compatible model endpoint and Langfuse. It is
not required for indexing, routing, adapters, evidence review, or serving. Provider configuration is
documented in [`.env.example`](.env.example); never set an API key unless pay-per-token execution is
intentional.

## Behavioral Skill CI

Behavioral CI supports improvement; it is not a benchmark-only product.

Every improvement writes:

```text
runs/evidence/<skill>/<run>/evidence.json
runs/evidence/<skill>/<run>/EVIDENCE.md
```

Evidence records:

- champion and challenger revisions;
- dataset, harness, and model identity;
- per-case and aggregate outcome deltas;
- input/output token deltas;
- changed components and gate reasons;
- scrubbed tool-order trajectories and first divergence.

Run routing fixtures independently:

```bash
skill-router eval evals/routing.yaml --root evals/fixtures/skills
```

Description changes cannot promote when held-out tasks stop routing to the intended skill. Execution
wins alone are insufficient.

## Promotion safety

- Improvement output stays under `runs/pending/`.
- `promote` requires a passing Behavioral CI gate.
- Evidence champion revision must still equal the live revision.
- Evidence challenger revision must equal the proposed content.
- Previous live content is snapshotted under `runs/revisions/`.
- Promotion stages a complete directory, swaps it into place, and rolls back if the swap fails.
- The read-only server notices the revision change and rebuilds its immutable registry.
- Traversal and symlink escapes are rejected.

`runs/` is an operator-only trust boundary: promotion revalidates content revisions, but it consumes
the recorded gate verdict rather than rerunning paid evaluation. Do not grant untrusted writers
access to pending evidence.

Literal prompt-injection patterns remain a cheap prefilter only. Optional semantic classification and
human review provide the stronger layers.

## Packages and dependencies

One distribution keeps first-run simple:

- Base: registry, local embeddings, routing, CLI, stdio MCP.
- `skill-router[optimizer]`: GEPA, full-agent A/B, Langfuse/model plumbing.
- `skill-router[ui]`: approval UI.
- `skill-router[guard]`: optional semantic injection classifier.
- `skill-router[dev]`: tests and package verification.

No Docker, model API key, hosted tracing, or demo agent is required for routing.

## Third-party skills

Skill Router does not compete with skill installers. Use `npx skills`, `gh skill`, or another trusted
installer to populate an external review root, then index that root.

The old mutable-branch fetch script is disabled. `scripts/fetch_skills.sh` fails closed until
`skills.lock.json` contains reviewed commit pins, content hashes, provenance, and license metadata.
Never fetch an unlocked source directly into a live indexed library.

## CARN

Structural divergence records tool order and argument names, not semantic equivalence. CARN or a
model-graded layer may add deeper trajectory interpretation.

The OSS evidence schema is the boundary to CARN, SlanchaAI’s enterprise add-on. Skill Router owns the
single-developer loop: route, learn, improve, gate, approve, promote. CARN can add fleet trajectory
tries, cross-run clustering, stuck detection, rescue analysis, governance, and organization-wide
dashboards without changing the OSS promotion contract.

## Development

```bash
python -m pip install -e '.[optimizer,ui,dev]'
pytest -q
```

Launch targets:

- Recall@3 at least 95% on real held-out prompts.
- No-route precision at least 95%.
- Warm routing p95 below 100 ms, network disabled.
- Bootstrap startup overhead below 500 measured tokens.
- Claude/Codex parity for compatible fixtures.
- No live mutation through the default MCP surface.

The repository license currently names James Maki as copyright holder. Ownership and public-release
attribution must be confirmed with James and SlanchaAI before publishing a release artifact.
