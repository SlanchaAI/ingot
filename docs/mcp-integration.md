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

### Claude Code and Codex setup scripts

The repository includes user-level setup scripts for Claude Code and Codex. Both scripts register
Ingot as a streamable HTTP MCP server at `http://localhost:8000/mcp` and install the official
Langfuse observability connector for that agent.

Start the services first:

```bash
docker compose up -d
curl http://localhost:8000/mcp
curl http://localhost:3100/api/public/health
```

The first `curl` may return an MCP protocol error because it is a plain GET without an MCP session;
that still confirms the server is listening. The Langfuse health request should succeed. If either
command reports connection refused, wait for Compose to finish starting and check
`docker compose ps` before configuring an agent.

For Claude Code, install Claude Code plus `uv` (recommended). The fallback is Python 3.10 or newer
with `pip` and `langfuse>=4.0,<5`. On macOS:

```bash
brew install uv
./scripts/claude_setup.sh
```

The plugin uses `uv` to provision its pinned SDK environment automatically. Without `uv`, the setup
script verifies or installs `langfuse>=4.0,<5` into the selected Python environment. It also adds
the user-level `ingot` MCP server and installs the Langfuse Claude Observability Plugin. Restart
Claude Code after setup. The install command configures the plugin from `LANGFUSE_BASE_URL`,
`LANGFUSE_PUBLIC_KEY`, and `LANGFUSE_SECRET_KEY`, or uses the bundled local defaults.

For Codex, install Codex 0.128 or newer, Node.js 22 or newer, and Python 3. Python is used only to
write the private JSON configuration. On macOS with Homebrew:

```bash
brew install node@22
brew install python@3.12  # skip when `python3 --version` already works
node --version  # must report v22 or newer
./scripts/codex_setup.sh
```

The Codex script adds the user-level `ingot` MCP server, installs and enables the Langfuse tracing
plugin, and writes its credentials to `~/.codex/langfuse.json` with mode `0600`. It defaults to the
bundled local Langfuse. Point it at another project by setting `LANGFUSE_BASE_URL`,
`LANGFUSE_PUBLIC_KEY`, and `LANGFUSE_SECRET_KEY` for the setup command. Set `INGOT_MCP_URL` for
either script when Ingot is not running on localhost.

Both scripts are idempotent: an already-correct MCP registration, marketplace, plugin, and SDK are
left alone. Diagnose an incomplete setup without changing it, or repair the state managed by a
script, with:

```bash
./scripts/claude_setup.sh --doctor
./scripts/claude_setup.sh --repair
./scripts/codex_setup.sh --doctor
./scripts/codex_setup.sh --repair
```

`--doctor` reports dependency versions, connector state, local configuration, and Langfuse health,
then exits nonzero when required installed state is missing. `--repair` replaces an `ingot` MCP
entry whose URL differs from `INGOT_MCP_URL`, upgrades required dependencies, and reinstalls the
managed observability plugin. If a normal setup command fails, its error points to these modes.

### Agent on another LAN machine

Run the setup script on the machine where Claude Code or Codex runs, and use URLs that machine can
reach. For example, if the Ingot host is `192.168.1.40`:

```bash
# On the remote Claude Code machine
INGOT_MCP_URL=http://192.168.1.40:8000/mcp ./scripts/claude_setup.sh

# On the remote Codex machine
INGOT_MCP_URL=http://192.168.1.40:8000/mcp \
LANGFUSE_BASE_URL=https://192.168.1.40:3443 \
LANGFUSE_PUBLIC_KEY=pk-lf-... \
LANGFUSE_SECRET_KEY=sk-lf-... \
./scripts/codex_setup.sh
```

The default Compose MCP publish is deliberately loopback-only. On the Ingot host, expose only the
MCP port on a trusted interface with a small override file:

```yaml
# compose.agent-lan.yml
services:
  mcp:
    ports: !override
      - "192.168.1.40:8000:8000"
```

Start it with `docker compose -f docker-compose.yml -f compose.agent-lan.yml up -d`. For Langfuse,
follow the secure LAN setup in [Security](security.md#network-exposure), including rotated secrets,
the `langfuse-lan` TLS profile, and trusting Caddy's local CA on the agent machine. Do not use a
`localhost` Langfuse URL in a remote setup, because that points back to the agent machine. MCP has
no built-in authentication, so do not publish port 8000 to an untrusted network or the internet.

After setup, restart the agent and tell it to call `ingot.route_and_load` once at the start of each
request. Registration exposes the tool but does not force the agent to use it.

### Make skill loading part of the agent instructions

Installing the MCP server only makes its tools available. Claude Code and Codex may answer directly
unless the project tells them to route first. Put this instruction in the repository's persistent
agent file (`CLAUDE.md` for Claude Code and `AGENTS.md` for Codex):

```text
At the start of every user request, call ingot.route_and_load exactly once with the complete user
task and the current harness and workspace context. If it returns match or related_match, follow the
returned skill_body while completing the request. If it returns novel, continue without a skill.
Do not merely list or suggest the skill: load it and apply it before doing the task.
```

Use the same rule in organization-managed agent instructions if repositories should not carry local
agent files. After enrollment, verify behavior with a harmless request and confirm both the
`route_and_load` tool call and final answer appear in Langfuse. A successful `--doctor` result proves
the connector is installed and reachable, but it does not prove the model followed this instruction.

For an authenticated live check, start the bundled stack and run
`./scripts/claude_langfuse_smoke.sh` or `./scripts/codex_langfuse_smoke.sh`. Each script makes one
real model request, requires a completed `route_and_load` call, waits for the matching Langfuse
trace, and passes that trace through the mining parser. These are opt-in checks because they use an
external agent account and are not suitable for ordinary unit CI.


## Tracing from your own harness (MCP only)

Trace mining reads from Langfuse over its public API; it does not care which harness or SDK wrote
the traces, only that each one meets a small contract. That contract is what lets any Langfuse
integration feed mining: the LangChain / LangGraph callback, the OpenAI and Anthropic drop-in
wrappers, LiteLLM, LlamaIndex, the TypeScript SDK, or raw OpenTelemetry all work, whatever your
agent is written in.

**The contract is on the trace root.** Mine reads each trace's top-level `input` and `output` to
select and grade it; child spans from an auto-instrumenting connector are ignored for selection
(they still give you the rich per-call detail in the Langfuse UI). The root must match one of three
shapes:

1. **Explicit**: `input = {"task": "<user request>"}` (optionally `{"task", "rubric"}`) and
   `output` a plain answer string. Connector-agnostic: set these on the root span yourself and any
   harness qualifies.
2. **LangGraph state**: `input = {"messages": [...]}` and `output = {"messages": [...]}`; mine
   takes the first message as the task and the last as the answer. The bundled `agent/run.py` and
   any harness using the Langfuse LangChain `CallbackHandler` produce this automatically.
3. **Coding-agent connector**: a plain-string input and output, as emitted by the Codex connector,
   or `{ "role": "user", "content": "..." }` and the matching assistant object emitted by the
   Claude Code connector. The setup scripts below install these connectors.

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
   `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` at it and use the external-Langfuse Compose
   override so the bundled containers do not start. See
   [Using your own Langfuse project](configuration.md#using-your-own-langfuse-project).
3. **A different platform** (Arize Phoenix, etc.): **not wired yet.** The write side already works
   for any platform: it's OpenTelemetry, and so is Langfuse, so your harness can export spans
   anywhere. The *read* side is the gap: mining pulls traces from Langfuse's public trace API
   (`GET /api/public/traces`), and that HTTP call lives in exactly one place, `fetch_traces()` in
   `optimize/mine.py`. Supporting another platform means adding an adapter there that returns the
   same `{task, rubric, answer, tags}` shape from that platform's API and support paginated reads;
   everything downstream (local clustering, cached representative judging, dimension aggregation,
   mined candidates) is backend-agnostic. First-class adapters for
   other platforms are planned; until then, option 2 (a Langfuse your platform can forward to, or a
   Langfuse-compatible endpoint) is the supported path.
