# Architecture

Ingot is a trusted, local-first skill routing and improvement system. It is not a multi-tenant
service. The default Compose deployment exposes only loopback ports.

## Components and data flow

1. The bundled agent sends task and execution context to `route_and_load` over MCP.
2. The router refreshes the skill registry when files change, filters by harness, platform, scope,
   tools, MCPs, activation, and trust, then ranks compatible descriptions. Description embeddings
   are cached across refreshes.
3. One response is authoritative for the direct `match` or explicit `related_match`, loaded body,
   revision, root, body-free alternatives, and `novel` escalation signal. A related match is loaded
   for compose-or-extend use. The agent uses the weak model unless `novel` is true.
4. The run can be recorded in local JSONL and, when configured, Langfuse. Hosted model calls use
   the configured OpenAI-compatible endpoint. OpenRouter calls always request ZDR providers.
5. Mining selects difficult, semantically diverse failures. Optimization writes a quarantined
   challenger. The UI presents evidence and an explicit approval action atomically promotes it.

## Skill lifecycle and invariants

Skill folders are loaded from configured roots. Third-party and directly edited folders are
operator-trusted input. Agent creation and optimizer rewrites enter `runs/pending`; they never
become routable without approval. Promotion checks identity, safety, collision, evidence, and
revision freshness, snapshots the prior revision, and swaps directories atomically. Approval and
rollback actions append metadata-only audit records to `runs/approval-audit.jsonl`.

The key invariants are:

- `route_and_load` is the only serving selection contract.
- At most one skill body crosses the router boundary. Alternatives contain no bodies.
- Compatibility filtering happens before ranking or model selection.
- Pending content is inactive, and promotion is explicit.
- A revision identifies all files in a skill folder.
- Hosted credentials are read from environment variables and are never stored in traces.

## Stores and ownership

`skills/` contains active skills. `optimize/tasks/` contains eval sets. `runs/pending/` contains one
active review slot per skill, with displaced candidates archived. `runs/revisions/` contains
rollback snapshots. `runs/traces.jsonl` contains local traces. Langfuse uses its own Compose
Postgres, ClickHouse, and object stores.

Local JSON and JSONL stores assume one logical writer per skill or file. Atomic replacement protects
pending records and skill promotion, but concurrent optimizer, approval, or rollback processes are
not a supported coordination mechanism. The approval UI serializes optimization in one process.
Trace appends use one operating-system append write per record; operators should still mount a
single local writer when using network filesystems.

Local trace schema version 1 contains `schema_version`, Unix `ts`, `task`, `answer`, and `tags`.
Readers also accept the original unversioned shape as version 1. See README privacy controls for
opt-out, redaction, permissions, and rotation.

## Deployment modes

Lite mode runs MCP and the UI, with one-shot agent and optimizer containers on demand. The optional
`langfuse` profile adds local observability services. Fully local inference points model URLs to
vLLM or Ollama. Hosted inference sends prompts and outputs to the selected provider. None of these
modes adds endpoint authentication, so non-loopback publishing requires an authenticating proxy and
authorization appropriate to the available tools.

## Failure and recovery

MCP refresh compares file signatures and rebuilds only after change; unchanged description vectors
are reused. A failed refresh keeps the previous state until a later request retries. Trace and
Langfuse failures do not fail serving. Malformed local trace lines are skipped by mining.

Promotion stages changes and restores the prior directory if the swap fails. Every rewrite stores
the displaced revision. Restore it with:

```bash
docker compose run --rm optimize python -m optimize.promote rollback SKILL REVISION
```

Operators should back up `skills/`, `runs/`, and `optimize/tasks/`. Container databases require
normal volume backup procedures.

## Trust boundaries

The host Docker socket grants near-root authority over the host. It is mounted only into on-demand
optimization and approval services for execution-grounded judging. Do not run those services for
untrusted users. Prefer a dedicated Docker context or isolated host, and omit the socket when using
static checks.

MCP and UI are unauthenticated. Skills can contain hostile instructions or executable assets.
Hosted providers receive model traffic. `CARN_DIR` dynamically imports Python selected by the local
operator. Local traces can contain task and answer text. These boundaries and concrete deployment
safeguards are expanded in [SECURITY.md](SECURITY.md).
