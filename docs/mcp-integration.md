# Bring your own agent (MCP)

### Bring your own agent (MCP only)

Most deployments use just the MCP server with their own harness (Claude Code, Codex, a custom
agent); the bundled `agent/run.py` is a reference client, not a requirement. Point your harness at
`http://localhost:8000/mcp` and call `route_and_load` once per request:

- **`match`**: a direct match. Follow `skill_body`; a weak/cheap model suffices.
- **`related_match`, `novel: false`**: the closest compatible skill is below the direct-match
  threshold. Its identity, revision, root, and sole body are loaded so the weak model can compose
  or extend it. `alternatives` remain body-free.
- **`novel: true`**: nothing even related. Serve with your strong model.

To keep the trace-mining loop fed from your own harness, see
[Tracing from your own harness](#tracing-from-your-own-harness-mcp-only).


## Tracing from your own harness (MCP only)

Trace mining reads from Langfuse over its public API; it does not care which harness or SDK wrote
the traces, only that each one meets a small contract. That contract is what lets any Langfuse
integration feed mining: the LangChain / LangGraph callback, the OpenAI and Anthropic drop-in
wrappers, LiteLLM, LlamaIndex, the TypeScript SDK, or raw OpenTelemetry all work, whatever your
agent is written in.

**The contract is on the trace root.** Mine reads each trace's top-level `input` and `output` to
select and grade it; child spans from an auto-instrumenting connector are ignored for selection
(they still give you the rich per-call detail in the Langfuse UI). The root must match one of two
shapes:

1. **Explicit**: `input = {"task": "<user request>"}` (optionally `{"task", "rubric"}`) and
   `output` a plain answer string. Connector-agnostic: set these on the root span yourself and any
   harness qualifies.
2. **LangGraph state**: `input = {"messages": [...]}` and `output = {"messages": [...]}`; mine
   takes the first message as the task and the last as the answer. The bundled `agent/run.py` and
   any harness using the Langfuse LangChain `CallbackHandler` produce this automatically.

**Attribution tags** (recommended): tag the trace with the served skill's name plus
`revision=<name>@<revision>`, and `novel` when you escalated to your strong model. Untagged traces
fall back to embedding relevance on the task text.

Most non-LangChain connectors log a provider-native request/response that matches neither shape, so
the portable recipe for any harness is to wrap the served turn in one root span whose `input` /
`output` you set to shape 1, and let the connector's auto-instrumentation nest underneath:

```python
# Python (Langfuse SDK v4). The same pattern applies in the TypeScript SDK or via OpenTelemetry:
# open a root span, set input/output to shape 1, tag it, and let your provider connector nest below.
from langfuse import get_client

lf = get_client()  # LANGFUSE_BASE_URL / _PUBLIC_KEY / _SECRET_KEY, same values as the compose stack
r = route_and_load(task, harness="claude", cwd=cwd)             # via MCP
selected = r["match"] or r["related_match"]
tags = ([selected, f"revision={selected}@{r['revision']}"] +
        (["related"] if r["related_match"] else [])) if selected else ["novel"]
with lf.propagate_attributes(tags=tags):
    with lf.start_as_current_observation(name="serve", input={"task": task}) as span:
        answer = my_agent(task, r["skill_body"])                # your harness, your models,
        span.update(output=answer)                              # any Langfuse connector nests here
```

For a LangChain / LangGraph harness you can skip the manual span entirely: pass the Langfuse
`CallbackHandler`, and the `{"messages": [...]}` state satisfies shape 2 on its own; attach the same
tags through the handler's metadata. For a non-Python agent, do the same with the TypeScript SDK, or
export OpenTelemetry spans to Langfuse's OTel endpoint and set the root span's input/output to shape
1. The selection contract is identical in every case.

With traces flowing, `docker compose run --rm optimize-mine <skill>` and the background loop work
unchanged. Two caveats: mining re-judges traffic with `JUDGE_MODEL` (on your API bill), and
candidate rollouts still execute on the bundled scaffold, so set `AGENT_MODEL` to your production
serving model.


## Using your own evals platform

Langfuse is the **default and required** evals backend: it comes up with `docker compose up`, and
trace mining has no local fallback (`optimize-mine` fails loudly if no Langfuse-compatible endpoint
is reachable, rather than returning an empty result that would read as "nothing failing"). You have
three options:

1. **Bundled Langfuse** (default): self-hosted in the compose stack, nothing to configure. Secure
   its demo credentials before exposing it: [Securing the Langfuse deployment](security.md#securing-the-langfuse-deployment).
2. **Your own Langfuse**: Cloud or self-hosted elsewhere. Point `LANGFUSE_BASE_URL` /
   `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` at it; the bundled containers keep running unless
   you stop them. See [Using your own Langfuse project](configuration.md#using-your-own-langfuse-project).
3. **A different platform** (Arize Phoenix, etc.): **not wired yet.** The write side already works
   for any platform: it's OpenTelemetry, and so is Langfuse, so your harness can export spans
   anywhere. The *read* side is the gap: mining pulls traces from Langfuse's public trace API
   (`GET /api/public/traces`), and that HTTP call lives in exactly one place, `fetch_traces()` in
   `optimize/mine.py`. Supporting another platform means adding an adapter there that returns the
   same `{task, rubric, answer, tags}` shape from that platform's API; everything downstream (the
   judge, dimension aggregation, mined candidates) is backend-agnostic. First-class adapters for
   other platforms are planned; until then, option 2 (a Langfuse your platform can forward to, or a
   Langfuse-compatible endpoint) is the supported path.
