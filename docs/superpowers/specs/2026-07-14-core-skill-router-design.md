# Core Skill Router design

Date: 2026-07-14
Status: approved direction for Friday open-source launch
Branch: `launch/core-skill-router`

## Outcome

Skill Router becomes the shared discovery and loading layer for Claude and Codex. It replaces the
native pattern of injecting an entire skill catalog into every turn. Each harness exposes one small
bootstrap skill and one read-only routing tool; the router keeps the full library outside both
harnesses and injects only the selected skill body.

Routing is the start of the product loop, not the whole product. Skill Router observes outcomes for
the exact routed revision, mines failures, proposes a challenger, runs routing and execution checks,
and keeps the challenger quarantined until a human approves promotion. Behavioral CI is the trust
layer inside improvement; it is not the product's primary positioning or a benchmark-only workflow.

**Positioning:** Route to the right skill. Learn from failures. Improve it safely.

The launch claim is concrete: reduce the current approximately 102-entry, 7,500-token startup
catalog to less than 500 startup tokens, with skill instructions entering context only after a
route matches.

```mermaid
flowchart LR
    T["Task"] --> R["Route + load"]
    R --> C["Claude / Codex"]
    C --> O["Observed outcome"]
    O --> I["Improve skill"]
    I --> B["Behavioral CI gate"]
    B -->|passes + approved| P["Promote revision"]
    B -->|fails| I
```

## Product boundary

The open-source product does seven things:

1. Index Agent Skills from configurable external roots.
2. Filter and rank skills for a task and execution environment.
3. Return one selected body through a single read-only tool call.
4. Give Claude and Codex the same routing policy through thin adapters.
5. Attribute observed outcomes to the exact routed skill revision.
6. Mine failures and propose targeted skill challengers.
7. Gate promotion with routing and behavioral evidence plus human approval.

The local router has no model or hosted-service requirement. Improvement dependencies remain an
optional install extra, but `improve`, `review`, and `promote` are first-class product workflows—not
separate positioning. The approval UI, demo agent, and semantic guard remain optional interfaces.
CARN remains a separate enterprise/add-on consumer of the same revisioned evidence contract.

## First-run contract

```bash
pip install skill-router
skill-router index ~/.agents/skills
skill-router route "debug this failing hook"
skill-router serve --stdio
```

These commands require no Docker, API key, hosted service, or tracing backend. Indexing may download
the configured local embedding model once; routing is then warm, local, and network-free.

For Friday, `skill-router` is one PyPI distribution. The source is separated into core, MCP,
optimizer, UI, demo-agent, and guard modules, while optional dependency groups keep the base install
small. The base distribution includes the core CLI and stdio MCP path so the four-command first run
works exactly as shown. Extras are:

- `skill-router[optimizer]`: GEPA, Langfuse, and model-provider plumbing.
- `skill-router[ui]`: approval UI.
- `skill-router[demo-agent]`: LangGraph/OpenRouter demonstration agent.
- `skill-router[guard]`: optional semantic injection classifier.

The full improvement loop is:

```bash
skill-router improve pdf
skill-router review pdf
skill-router promote pdf
```

`improve` mines evidence for the exact current revision, proposes a quarantined challenger, and runs
the behavioral gate. `review` never mutates the live library. `promote` refuses challengers that have
not passed the gate and always requires an explicit human action.

Separate `skill-router-core` and `skill-router-mcp` distributions are deferred because they would
make the launch install story worse without changing runtime boundaries.

## External registry

The registry accepts roots from repeated CLI flags and `SKILL_ROUTER_PATHS`. CLI flags take
precedence; the environment variable uses the platform path separator. Roots are expanded,
resolved, deduplicated, and searched in declared order.

```text
SKILL_ROUTER_PATHS=~/Source/dotfiles-claude/skills:~/.agents/skills:~/Source/shared-agent-skills
```

The repository's existing `skills/` directory remains a default only when it contains skills. The
router never copies the external library into a native harness directory. Duplicate skill names are
an index error unless an explicit root-precedence rule selects one; they never collapse silently.

Each indexed revision records:

- canonical skill root and `SKILL.md` path;
- normalized frontmatter and content hash;
- source root and precedence;
- index timestamp and embedding model identity;
- validation and trust state.

The index cache lives in the platform user-cache directory, never beside the source library.

## Routing metadata

Standard Agent Skills fields remain valid. Skill Router adds optional namespaced metadata under
`metadata.skill-router`:

```yaml
metadata:
  skill-router:
    harnesses: [claude, codex]
    scopes: [global, project]
    path_patterns: ["**/*.py", "pyproject.toml"]
    required_tools: [bash]
    required_mcps: []
    trust: reviewed
    activation: automatic
    platforms: [macos, linux]
    priority: 50
    conflicts: []
```

Defaults preserve ordinary Agent Skills compatibility: both harnesses, global scope, every platform,
no required tools or MCPs, `unknown` trust, automatic activation, priority 50, and no conflicts.
Unknown extension fields round-trip but do not affect routing.

Filtering happens before similarity ranking:

1. harness compatibility;
2. platform compatibility;
3. global/project/path scope against canonical `cwd`;
4. required tool and MCP availability;
5. trust policy and manual-only activation;
6. declared conflicts;
7. embedding similarity, then priority and stable name ordering as tie-breakers.

Manual-only skills are never returned for automatic routing. They may be selected only when the
caller supplies an exact skill name through the administrative CLI, not through the model-facing
tool.

## One-call MCP surface

The model-facing server exposes one tool:

```text
route_and_load(task, harness, cwd, available_tools=[], available_mcps=[])
→ {
    match,
    score,
    reason,
    skill_body,
    skill_root,
    revision,
    alternatives
  }
```

`match` is null and `skill_body` is empty when no candidate crosses the configured threshold.
`alternatives` contains at most two names, scores, and reasons—never additional bodies. `reason` is
a deterministic explanation assembled from matched filters and score; it is not model-generated.

This replaces the current model-facing `suggest_skills` then `get_skill` sequence. `list_skills`,
index refresh, exact-name inspection, authoring, and promotion become admin/CLI surfaces and are not
registered in the default MCP server.

The read-only call has no filesystem mutation path. Hot reload swaps a fully built immutable registry
under a lock only after the replacement index validates successfully.

## Transport and adapters

### Stdio default

`skill-router serve --stdio` is the default and recommended local transport. HTTP requires an
explicit `--http` flag and defaults to `127.0.0.1`. Non-loopback HTTP is rejected unless the operator
also supplies an authentication configuration. Wildcard hosts and origins are never defaults.

### Claude adapter

The Claude adapter contains:

- one small bootstrap instruction: route nontrivial tasks before execution;
- one MCP server definition invoking `skill-router serve --stdio`;
- installation documentation that moves the full library out of native discovery.

It does not run a UserPromptSubmit embedding hook and does not inject the catalog. Native discovery
must be disabled or left with only the bootstrap skill by the installer; the adapter verifies and
reports remaining native skills rather than deleting them.

### Codex adapter

The Codex adapter is one plugin containing the same bootstrap skill and stdio MCP definition. It
passes `harness=codex` and the current tool/MCP inventory. Harness-specific skill variants are stored
inside the shared skill package and selected only after the common route has chosen the skill.

Both adapters call the same router and therefore select the same revision whenever compatibility
metadata is equal.

## Harness-specific bodies

The default body remains `SKILL.md`. A skill may optionally provide:

```text
variants/claude.md
variants/codex.md
```

The registry validates variant paths remain inside the skill root. A matching variant replaces only
the instruction body; identity, description, trust, scope, and revision remain shared. Missing
variants fall back to `SKILL.md`.

## Safe lifecycle

The public default is read-only. Existing mutation behavior changes as follows:

- `create_skill` is absent from the default MCP tool surface.
- Explicitly enabled authoring writes to a quarantine root outside the live indexed roots.
- Quarantined skills must pass structural, path, collision, and content checks before human review.
- Promotion uses an atomic temporary-file write and rename, creates an immutable revision record, and
  keeps the previous revision available for rollback.
- `write_components` rejects absolute paths, traversal, symlink escapes, and writes outside the
  canonical skill root.
- Regex content checks are named and documented as literal-pattern prefilters. They are never
  represented as semantic prompt-injection detection.

Trace mining and promotion evidence key every event to the exact skill revision. Routing-description
changes must pass retrieval evaluation as well as task execution evaluation before promotion.

## Supply chain

Third-party fetches consume a committed lockfile containing source URL, commit SHA, selected skill
paths, content checksums, provenance, and license. Fetching an unlocked source or a checksum mismatch
fails closed. Updates are explicit lockfile diffs; the launch path never clones a mutable default
branch and immediately activates its contents.

Before publishing, repository ownership and license attribution must be corrected or confirmed. The
current MIT notice naming only James Maki is not silently rewritten; the PR calls out the required
owner decision.

## CLI behavior and errors

- `index PATH...`: validates roots, builds a complete replacement index, prints counts and excluded
  reasons, then atomically swaps the cache. Any duplicate identity or invalid canonical path fails
  the command without replacing the prior good index.
- `route TASK`: uses the cached index, prints the match, score, revision, reason, and alternatives.
  `--json` emits the same schema as MCP without terminal formatting.
- `serve --stdio`: loads the current cache or builds it from configured roots, then serves only
  `route_and_load`.
- `doctor`: reports native-catalog remnants, unavailable roots, stale indexes, missing harness tools,
  and unsafe HTTP configuration. It never changes harness configuration.

No match is a successful routing outcome with exit code 0. Invalid configuration, corrupt index,
duplicate identity, or unavailable required runtime dependencies return nonzero with a specific
remediation message.

## Improvement and Behavioral CI

Behavioral CI is invoked by `improve`; users do not need to build a benchmark system before the
router becomes useful. It emits portable evidence so the same decision can be reviewed locally, in
GitHub, in the approval UI, or by CARN.

Two suites gate every description/body promotion:

1. A committed, non-sensitive routing suite with positive, related, no-route, scope, platform,
   trust, tool, MCP, priority, conflict, and cross-harness cases.
2. A local scrubbed suite derived from real prompts against the current 102-entry library. Only
   aggregate metrics and the generation method are committed.

Description evaluation measures top-1, recall@3, no-route precision, and collision/regression rates.
Task execution evaluation compares champion and challenger outcomes, objective checks, judge scores,
token cost, and the first available trajectory divergence. A description or body cannot promote when
either suite regresses beyond configured tolerance. Every artifact records skill name, champion and
challenger revisions, dataset revision, harness, model, and gate reasons.

The OSS evidence schema contains aggregate and per-case results plus optional scrubbed trajectory
events. CARN may add fleet aggregation, full trajectory tries, stuck detection, rescue analysis,
governance, and private dashboards without changing router behavior or the OSS promotion contract.

## Success gates

- Recall@3 at least 95% on the scrubbed real-prompt suite.
- No-route precision at least 95%.
- Warm routing p95 below 100 ms with network disabled.
- Bootstrap/native catalog startup overhead below 500 measured tokens.
- No native catalog truncation because the native catalog contains only the bootstrap.
- Claude and Codex select the same revision for every parity fixture.
- No live skill mutation through the default MCP surface.
- `pip install skill-router` followed by the four-command first run succeeds on a clean supported
  Python environment.
- Existing optimizer and UI tests remain green when their extras are installed.

## Friday implementation order

1. Package boundary, configurable roots, validation, immutable index, and CLI.
2. Filtered routing and `route_and_load` with stdio-first transport.
3. Committed routing suite plus local 102-skill benchmark harness.
4. Claude bootstrap adapter and measured startup-token proof.
5. Codex plugin adapter and cross-harness parity tests.
6. Quarantine-by-default authoring, contained writes, atomic revisions, and rollback.
7. Dependency extras, locked fetch pipeline, release metadata, and documentation.

## Explicit non-goals for this PR

- Hosted multi-tenant control plane or remote authentication service.
- Automatic edits to a developer's Claude or Codex configuration.
- Moving CARN implementation or proprietary artifacts into the open-source core.
- Additional model providers or harness adapters beyond Claude and Codex.
- Automatic promotion without human approval.
- A new semantic detector represented by regex or direct phrase matching.

## Verification strategy

- Unit tests for root precedence, duplicates, canonical containment, metadata defaults, every filter,
  tie-breaking, thresholds, variants, quarantine, revisions, rollback, and response schemas.
- Contract tests invoking the same route through CLI, stdio MCP, Claude adapter fixture, and Codex
  adapter fixture.
- Adversarial filesystem tests for traversal, absolute paths, symlink escapes, duplicate frontmatter,
  hostile variants, corrupt cache replacement, and locked-source drift.
- Performance test with the 102-entry index, warmed before measurement and network disabled.
- Fresh-environment installation smoke test using the built wheel, not the source checkout.
- Full existing test suite before every commit series and before PR creation.
