# Langfuse v4 read readiness

Ingot is not ready for the legacy trace-read cutover: mining and both connector
smoke scripts use the removed trace-list endpoint. The issue's
`optimize/mine.py:63` reference **does match current master** at audited commit
`9a91b98bece00f74bea67c20a8316c55c93c379d` (2026-10-22).

Tracks [SlanchaAI/ingot#45](https://github.com/SlanchaAI/ingot/issues/45), which
reports the Cloud cutover on November 16, 2026. This is a source audit, not a live
Cloud compatibility test; no application code or deployment changes were made.

## Sources and interpretation

- [Upgrade guide linked in #45](https://langfuse.com/self-hosting/upgrade/upgrade-guides/upgrade-v3-to-v4)
  [P, Langfuse upgrade guide, TBD: verify publication freshness]. Legacy trace,
  observation and session reads return 404 under v4's `events_only` mode.
  Transitional `legacy`/`dual` modes retain them. Datasets remain unaffected;
  dataset-run reads are a different, removed surface.
- [Public API: observations, fields and pagination](https://langfuse.com/docs/api-and-data-platform/features/public-api#observations-api-v2)
  [P, Langfuse API documentation, TBD: verify publication freshness]. Defines the
  replacement below. Both vendor pages were read on 2026-10-22; trust scores are
  left unassigned rather than inventing publication dates or confidence numbers.
- [Issue #45](https://github.com/SlanchaAI/ingot/issues/45)
  [P, issue report, TBD: verify Cloud schedule independently]. The reported Cloud
  date is context, not evidence that Ingot has already been migrated.

The bundled server is still v3.221.1 (`docker-compose.yml:238`,
`docker-compose.yml:280`). A healthy bundled stack therefore does not prove v4
readiness. `requirements.txt:16` pins the Python SDK to 4.14.0; server and SDK
major versions are separate contracts.

## Complete read inventory

Locations below refer to the audited commit. Searches covered tracked source,
shell scripts, Compose, documentation and tests for Langfuse imports/client
calls, HTTP readers and `/api/public` literals; call sites were then read.

| Location | Read and consumer | v4 impact and replacement |
| --- | --- | --- |
| `optimize/mine.py:63` | `fetch_traces()` GETs `/api/public/traces` with `limit=100&page=N`; consumes input, output and tags. Called by mining at `optimize/mine.py:366`. | Removed after cutover. Use root Observations API v2 query below; replace numeric paging and adapt response parsing. |
| `scripts/codex_langfuse_smoke.sh:36` | GET `/api/public/traces?limit=50`; polls for marker in input/output and asserts string roots. | Removed. Use root v2 query with `io`; preserve marker and shape checks after decoding. |
| `scripts/claude_langfuse_smoke.sh:49` | GET `/api/public/traces?limit=50`; polls for marker in input/output. | Removed. Same root v2 query and decoding. |
| `scripts/codex_langfuse_smoke.sh:62` | Calls `fetch_traces(50)` for parser round trip. | Indirect legacy read; inherits miner migration. |
| `scripts/claude_langfuse_smoke.sh:71` | Calls `fetch_traces(50)` for parser round trip. | Indirect legacy read; inherits miner migration. |
| `optimize/ab.py:400` | SDK `langfuse.get_dataset(ds_name)` loads holdout dataset/items; `_holdout_dataset()` caller at `optimize/ab.py:478`. | Dataset read, not dataset-run read. Keep `get_dataset`; guide does not remove it. Loaded items feed `dataset.run_experiment()` at `optimize/ab.py:286` and `item.input` at `optimize/ab.py:266`. No experiment-results read occurs here. |
| `optimize/__init__.py:71` | GET `/api/public/health`; decides whether A/B experiment logging is available (`optimize/ab.py:391`). | No removal/change identified in guide. Keep health probe; it does not establish trace-read compatibility. |
| `scripts/codex_setup.sh:65` | Doctor GET `/api/public/health`. | No change identified; keep endpoint. |
| `scripts/claude_setup.sh:69` | Doctor GET `/api/public/health`. | No change identified; keep endpoint. |
| `scripts/codex_langfuse_smoke.sh:14` | Preflight GET `/api/public/health`. | No change identified; subsequent trace check still needs migration. |
| `scripts/claude_langfuse_smoke.sh:14` | Preflight GET `/api/public/health`. | No change identified; subsequent trace check still needs migration. |
| `scripts/compose_smoke.sh:37`, `scripts/compose_smoke.sh:38` | HTTP and TLS GET `/api/public/health` via `wait_for()` (`scripts/compose_smoke.sh:14`). | No change identified; retain both probes. |
| `docker-compose.yml:304` | Container healthcheck GET `/api/public/health` via wget. | No change identified; retain probe. |

Documentation also instructs health reads at `README.md:209`,
`docs/mcp-integration.md:29`, `PRODUCTION_SETUP.md:111` and
`PRODUCTION_SETUP.md:140`: same health assessment, no replacement identified.
`docs/mcp-integration.md:226` describes the legacy trace read and claims the HTTP
call lives in one place; the two smoke-script calls above also exist.

`tests/test_mine.py:25`, `:39`, `:62`, `:81`, `:93`, `:104` and `:138` exercise
`fetch_traces` with mocked HTTP or URL rejection. They are contract fixtures,
not additional live endpoints. `tests/test_setup_scripts.py:197` and `:198`
check the smoke scripts' miner imports.

No other SDK remote-read calls or raw Langfuse data endpoints were found.
`agent/run.py:91` (callback), `agent/run.py:265` (flush), dataset creation at
`optimize/ab.py:397`/`:399`, experiment execution at `optimize/ab.py:286` and
flush at `optimize/ab.py:485` are producers, not read endpoints. External
observability plugins installed by setup scripts are not vendored here; their
internal calls are outside this repository inventory.

## Replacement contract for all three direct trace readers

Use this request shape, with encoded values and explicit time bounds:

```text
GET /api/public/v2/observations?isRootObservation=true&limit=100&fields=core,basic,io,trace_context&fromStartTime=<start>&toStartTime=<end>
```

For one trace use `traceId=<id>`; add `isRootObservation=true` when only its root
is needed. SDK equivalent: `langfuse.api.observations.get_many(...)`.

- Pass returned `meta.cursor` as the next request's `cursor`; stop when absent
  or null. Remove `page`, `totalPages` and short-page termination assumptions.
- Default fields are only `core,basic`. Request `io` for input/output and
  `trace_context` for tags. Without them mining loses required task evidence.
- Rows are observations: `id` identifies the observation; `traceId` identifies
  the trace. Preserve this distinction in deduplication and attribution.
- Decode structured input/output strings before `_task_answer()`; preserve
  plain text. Do not request deprecated `parseIoAsJson=true` (400).

These replacements follow the Public API source above. Ingot-specific follow-up:
keep `limit=0` exhaustive across the chosen time window and positive limits
counting usable tasks, not raw rows (`optimize/mine.py:85`). Preserve tag routing
(`optimize/mine.py:133`), fail-loud behavior, and connector marker checks. Test
multi-page cursors, structured and plain roots, missing fields, duplicate roots,
and bounded newest-N behavior before shipping a code migration. Root selection
must not admit child tool spans as tasks.

No existing caller reads `/traces/{id}`, observations, sessions, scores, metrics
or dataset-run endpoints. Do not add replacement calls for unused surfaces.
Health and dataset-read findings mean “not removed by the cited guide,” not a
claim of observed v4 execution. Remaining work is the separate implementation
and live compatibility verification for mining and both connector smoke scripts.
