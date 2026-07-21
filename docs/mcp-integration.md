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

Trace mining reads from Langfuse over its public API; it does not care who wrote the traces. An
MCP-only deployment gets full mining parity by logging one trace per request that follows two
conventions:

1. **A shape mine can parse**: either trace `input = {"task": "<user request>"}` with `output` as
   a plain string, or a LangChain/LangGraph `{"messages": [...]}` state via the Langfuse
   `CallbackHandler`.
2. **Attribution tags** (recommended): tag the trace with the served skill's name plus
   `revision=<name>@<revision>`; tag `novel` when you escalated to your strong model. Untagged
   traces fall back to embedding relevance.

A minimal sketch (Langfuse Python SDK v4):

```python
from langfuse import get_client

lf = get_client()  # LANGFUSE_BASE_URL / _PUBLIC_KEY / _SECRET_KEY, same values as the compose stack
r = route_and_load(task, harness="claude", cwd=cwd)             # via MCP
selected = r["match"] or r["related_match"]
tags = ([selected, f"revision={selected}@{r['revision']}"] +
        (["related"] if r["related_match"] else [])) if selected else ["novel"]
with lf.propagate_attributes(tags=tags):
    with lf.start_as_current_observation(name="serve", input={"task": task}) as span:
        answer = my_agent(task, r["skill_body"])                # your harness, your models
        span.update(output=answer)
```

With traces flowing, `docker compose run --rm optimize-mine <skill>` and the background loop work
unchanged. Two caveats: mining re-judges traffic with `JUDGE_MODEL` (on your API bill), and
candidate rollouts still execute on the bundled scaffold, so set `AGENT_MODEL` to your production
serving model.
