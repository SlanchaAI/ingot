# Architecture

Ingot is a local-first change-control system for agent instructions. A skill folder is the unit of
change; every version of it is content-addressed, every proposed change is quarantined until a human
approves it, and every promotion is atomic and reversible. Routing exists to serve the approved
revision to an agent. Ingot is not a multi-tenant service, and the default Compose deployment
exposes only loopback ports.

## The change-control pipeline

1. A revision identifies the complete contents of a skill folder (`skill_revision`, a SHA-256 over
   every file, with the frontmatter and body normalized).
2. A proposed change enters `runs/pending/<skill>.json` and is inert there. Candidate rewrites
   use this one review slot per skill; a displaced candidate from a different pass is archived
   beside it.
3. A rewrite carries evidence, written to `runs/evidence/<skill>/<ts>/` as `evidence.json` and
   `EVIDENCE.md`. The body pass records held-out champion-vs-challenger scores, per-case deltas,
   the first behavioral divergence, token cost, the split's leakage status, and the gate verdict.
   The description pass records router metrics (top-1, recall@3, no-route precision,
   cross-harness parity) with the same revisions and gate verdict; it has no rollouts to diff.
4. The UI shows that evidence, the component diff, and one explicit approval action. Promotion
   re-checks the recorded gate verdict, that the champion revision still matches what is
   on disk, and that the challenger revision still matches the recorded evidence. It does not
   re-run collision checks or the held-out A/B.
   It then snapshots the revision it displaces into `runs/revisions/` and swaps directories
   atomically. The review card re-checks rewrite freshness when it renders, so a change whose
   champion has moved is refused before the approval click rather than after it.
5. Rollback restores any snapshot the same way, and also snapshots what it displaces.
6. Approval and rollback append metadata-only records to `runs/approval-audit.jsonl`. The actor is
   the approver's authenticated identity (HTTP Basic username or OIDC email), or `local-operator` in
   the zero-config open mode (see [SECURITY.md](SECURITY.md)).

## Serving path

1. The bundled agent sends task and execution context to `route_and_load` over MCP.
2. The router refreshes the skill registry when files change, filters by harness, platform, scope,
   tools, MCPs, activation, and trust, then ranks compatible descriptions. Description embeddings
   are cached across refreshes.
3. One response is authoritative for the direct `match` or explicit `related_match`, loaded body,
   revision, root, body-free alternatives, and `novel` escalation signal. A related match is loaded
   for compose-or-extend use. The agent uses the weak model unless `novel` is true.
4. The run is recorded to Langfuse (the default evals backend, or a Langfuse-compatible endpoint
   `LANGFUSE_*` points at); mining reads it back and has no local fallback. Hosted model calls use
   the configured OpenAI-compatible endpoint. OpenRouter calls always request ZDR providers.

## Candidate generation (optional)

Optimization proposes changes; it never makes them. It is off the review path and is expected to run
in the background (`optimize.loop`) or on demand from the UI.

Mining selects difficult, semantically diverse failures from real traces. The body pass runs one
candidate search, `optimize/skillopt_loop.py` (SkillOpt's reflective training loop, driven
per-component by `_greedy_search` in `optimize/ab.py`): bounded patch edits reflected from the seed's
judged failures and prior rejected edits, accepted only against a held-out strictly-improving gate.
The description pass (`optimize/routing.py`) scores
candidate descriptions with the real embedding router and uses GEPA only for its reflection step.
Both passes end at a quarantined pending record and an evidence bundle under `runs/evidence/`.

There is one candidate search, by design. A second, sequential GEPA body loop was removed: it
optimized the same objective at roughly twenty times the cost, was reachable only through an opt-in
flag, and had no test coverage of its own. `OPTIMIZE_STRATEGY` is no longer read, and the removed
pass flags (`--strategy`, `--gepa`, `--skip-gepa`, `--candidates`) fail the argument parse rather than
being silently ignored. The scripts pass (`--scripts`) optimizes bundled
`scripts/` files, but only when the skill's holdout carries execution-grounded `check:` assertions,
since the judge alone cannot tell a broken script from a working one; when it runs, both the
candidate rollouts and the A/B serve the assembled skill (body plus files), so a rewritten file is
actually executed by the evidence run. Other bundled files can still join the body pass as opt-in
text components (`OPTIMIZE_COMPONENTS=body,file:<path>`), diffed for review and not executed.

## Invariants

- `route_and_load` is the only serving selection contract.
- At most one skill body crosses the router boundary. Alternatives contain no bodies.
- Compatibility filtering happens before ranking or model selection.
- Pending content is inactive, and promotion is explicit.
- A revision identifies all files in a skill folder.
- Promotion and rollback are atomic, and each snapshots what it displaces.
- No dot-prefixed directory publishes a skill. Promotion and rollback stage into
  `.<name>.<hex>.stage` / `.previous` / `.rollback` beside the live directory, and discovery skips
  every dot-prefixed directory, so an abandoned stage cannot shadow an approved revision. This is
  the whole rule: a directory whose name is not a slug but does not start with a dot still
  publishes its `SKILL.md`, and takes its identity from the frontmatter `name`. The slug rule
  constrains which skills the application can act on (promotion, rollback, candidate
  generation, and the review surface all refuse a non-slug name), not which ones a library root
  can serve.
- Hosted credentials are read from environment variables and are never stored in traces.

## Stores and ownership

`skills/` contains active skills. `optimize/tasks/` contains eval sets. `runs/pending/` contains one
active review slot per skill, with displaced candidates archived. `runs/revisions/` contains
rollback snapshots, plus a `.snapshots.json` index per skill that records when each revision was
last snapshotted; it sits beside the snapshot directories, never inside one, so a rollback restores
the skill and nothing else. `runs/evidence/` contains evidence bundles, and pending records name
them relative to the repository root. `runs/approval-audit.jsonl` contains the approval trail.
Traces live in Langfuse, which uses its own Compose Postgres, ClickHouse, and object stores.

Local JSON and JSONL stores assume one logical writer per skill or file. Atomic replacement protects
pending records and skill promotion, but concurrent candidate, approval, or rollback processes are
not a supported coordination mechanism. Within its own process the UI serializes candidate
generation, and refuses a second approval or rollback with HTTP 409 while one is in flight, so two
clicks cannot interleave the snapshot, stage, and swap steps of a promotion.
Appends to the approval trail open the file with `O_APPEND` and loop until the whole record is
written, so a short write cannot leave a half record; readers skip unparseable and non-UTF-8 lines
either way. Trace appends use one operating-system append write per record. Operators should still
mount a single local writer when using network filesystems.

Local trace schema version 1 contains `schema_version`, Unix `ts`, `task`, `answer`, and `tags`.
Readers also accept the original unversioned shape as version 1. See README privacy controls for
opt-out, redaction, permissions, and rotation.

Pending records store candidate-search scores under `inner_loop`. Records written before the GEPA
body loop was removed used `gepa`; the review API reads either, so an existing queue still renders.

### Recovering a promotion killed between its two renames

A promotion or rollback swaps directories with two renames: the live skill moves to
`skills/.<name>.<hex>.previous`, then the staged directory moves into `skills/<name>`. A process
killed between them (SIGKILL, a container stop, a host crash) leaves no live directory, and the
skill's only copy in the `.previous` sibling. Discovery skips dot-prefixed names, so the skill stops
being served, and nothing restores it automatically: the staging sweep deliberately leaves a
`.previous` directory alone while the live directory is missing, because deleting it would destroy
that only copy. Recovery is a manual rename:

```bash
ls -d skills/.<name>.*.previous          # expect exactly one, and no skills/<name>
mv skills/.<name>.<hex>.previous skills/<name>
```

Any `.stage` or `.rollback` sibling left beside it is a discardable copy of something that still
exists, and the next promotion sweeps it. More than one `.previous` sibling means the store had more
than one writer, which is outside the model above: compare the directories and keep the one whose
revision the approval trail last recorded.

## Deployment modes

`docker compose up` runs MCP, the UI, and the self-hosted Langfuse evals backend, with one-shot
agent and candidate-generation containers on demand. Point `LANGFUSE_*` at an external Langfuse
(Cloud or self-hosted) to skip the bundled observability services. Fully local inference points model
URLs to vLLM or Ollama. Hosted inference sends prompts and outputs to the selected provider. Endpoint
auth is independent of these modes: the UI has its own password/OIDC gate (`AUTH_MODE`), but MCP has
none, so publishing MCP off-loopback requires an authenticating proxy and authorization appropriate to
the available tools.

## Failure and recovery

MCP refresh compares file signatures and rebuilds only after change; unchanged description vectors
are reused. A failed refresh keeps the previous state until a later request retries. Langfuse
callback failures do not fail serving, but mining fails loudly when the evals backend is unreachable
rather than returning an empty result that would read as "nothing failing".

Promotion stages changes and restores the prior directory if the swap fails. Every rewrite stores
the displaced revision. Restore it from the UI's History section, or with:

```bash
docker compose run --rm --entrypoint python optimize -m optimize.promote rollback SKILL REVISION
```

The `optimize` service's entrypoint is `python -m optimize.ab`, so the entrypoint override is what
makes the arguments reach `optimize.promote`.

Operators should back up `skills/`, `runs/`, and `optimize/tasks/`. Container databases require
normal volume backup procedures.

## Trust boundaries

The host Docker socket grants near-root authority over the host. It is mounted only into on-demand
candidate-generation and review services for execution-grounded judging. Do not run those services
for untrusted users. Prefer a dedicated Docker context or isolated host, and omit the socket when
using static checks.

MCP has no built-in authentication; the UI carries a password gate (or optional OIDC), unauthenticated
only in the zero-config open mode, where anyone who can reach it can approve a change or roll one back.
Skills can contain hostile instructions or executable assets. Hosted providers receive model traffic.
Local traces can contain task and answer text. These boundaries and concrete deployment safeguards
are expanded in [SECURITY.md](SECURITY.md).
