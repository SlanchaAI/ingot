"""LangGraph deep agent that uses the MCP skill router: it asks the router for skill suggestions,
loads the best skill's instructions, and follows them to solve the task. Prints the whole flow:
task -> proposed skills -> loaded skills -> result.

Env: API_KEY for the configured OpenAI-compatible endpoint (required unless MODEL_BASE_URL points
at a local endpoint), AGENT_MODEL (legacy alias MODEL), MODEL_BASE_URL (optional local vLLM/Ollama
OpenAI-compatible endpoint), STRONG_MODEL (serves novel no-skill tasks and attempts optional skill
authoring; defaults to GEPA_MODEL), MCP_URL, LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL
(optional tracing).
"""
import os
import sys
import asyncio
import json
import hashlib

from langchain_mcp_adapters.client import MultiServerMCPClient

MCP_URL = os.environ.get("MCP_URL", "http://mcp:8000/mcp")
# Endpoint + ZDR handling is shared with the optimizer (single source of truth): OpenRouter
# endpoints get the hardcoded zero-data-retention provider preference; MODEL_BASE_URL points this
# serving role at a local vLLM/Ollama server instead (README: Privacy).
from optimize import (ZDR_PROVIDER, agent_model, api_key, client_kwargs, model_api_key,  # noqa: E402
                      model_base_url, teacher_base_url)

MODEL = agent_model()


def strong_model() -> str:
    """Serve a request when no skill matches; optional authoring is separately gated by the MCP
    server. Defaults to the offline teacher (the GEPA skill author)."""
    return os.environ.get("STRONG_MODEL") or os.environ.get("GEPA_MODEL", "z-ai/glm-5.2")

INSTRUCTIONS = """You are a deep agent with access to a skill router over MCP.
For every task, first call `suggest_skills`, then decide from what it returns. Only ever call
`get_skill` with a name that `suggest_skills` returned — never guess skill names.

- If it returns a **direct match** (entries with no `related` flag): call `get_skill` on the top one,
  read it, and follow it.
- If it returns only **`related: true`** entries (no direct match): load the closest with `get_skill`
  and *compose or extend* it into your solution rather than authoring a duplicate.
- If it returns an **empty list** (nothing even related — a truly novel task): solve it from your own
  knowledge, then before your final answer you MUST call `create_skill` exactly once to attempt to
  persist a reusable skill distilled from your solution (description = one paragraph starting "Use
  this skill when..."; body = the general method/steps, not the specifics of this one request).
  This queues an inactive candidate for human review and must not affect the answer.

Prefer reusing/extending an existing skill over creating a new one. Keep the final answer concise.
Your final answer must contain the complete deliverable itself — e.g. full runnable code inline —
never just a description of, or reference to, files you created in your workspace: the user cannot
see your workspace."""


def build_agent(tools, instructions: str = INSTRUCTIONS, strong: bool = False):
    """The deep agent, wired to the given tools. Reused by the A/B optimizer and GEPA rollouts,
    which must stay on the weak MODEL (skills are optimized for the model that serves them) —
    only the serving entrypoint passes strong=True, for novel tasks with no skill to load."""
    from langchain_openai import ChatOpenAI
    from deepagents import create_deep_agent

    if strong:
        model = ChatOpenAI(model=strong_model(), temperature=0,
                           **client_kwargs(teacher_base_url(), key=api_key()))
    else:
        model = ChatOpenAI(model=MODEL, temperature=0,
                           **client_kwargs(model_base_url(), key=model_api_key(), reasoning=True))
    return create_deep_agent(model=model, tools=tools, system_prompt=instructions)


def langfuse_config(tags: list[str] | None = None, trace_id: str | None = None) -> dict:
    """ainvoke config with a Langfuse callback if keys are set, else empty. Pass `trace_id`
    (from Langfuse.create_trace_id()) to pin the run to a known trace so callers can attach
    scores to it afterwards."""
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return {}
    from langfuse.langchain import CallbackHandler
    handler = CallbackHandler(trace_context={"trace_id": trace_id}) if trace_id else CallbackHandler()
    return {"callbacks": [handler], "metadata": {"langfuse_tags": tags or []}}


def behavior_events(messages) -> list[dict]:
    """Scrubbed trajectory shape: tool order/argument names plus a digest of the final response."""
    events = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            events.append({"type": "tool", "name": call.get("name", "unknown"),
                           "arg_keys": sorted((call.get("args") or {}).keys())})
    final = messages[-1].content if messages else ""
    if isinstance(final, list):
        final = "\n".join(block.get("text", "") if isinstance(block, dict) else str(block)
                          for block in final)
    events.append({"type": "final", "sha256": hashlib.sha256(str(final).encode()).hexdigest(),
                   "characters": len(str(final))})
    return events


async def run_task(agent, task: str, config: dict | None = None, include_behavior: bool = False):
    """Run one task; returns final answer, routed skill revisions, and token usage.
    Usage sums usage_metadata over every LLM call in the run — the full cost of solving the task."""
    result = await agent.ainvoke({"messages": [{"role": "user", "content": task}]}, config=config or {})
    messages = result["messages"]
    loaded = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            if tc.get("name") == "get_skill":
                loaded.append(tc.get("args", {}).get("name", "?"))
        content = getattr(m, "content", None)
        if isinstance(content, list):  # MCP tool results arrive as content blocks
            content = "\n".join(block.get("text", "") if isinstance(block, dict) else str(block)
                                for block in content)
        if isinstance(content, str) and content.startswith("# Skill: "):
            # get_skill's header carries name@revision — upgrade the bare name from the tool call
            identity = content.split("\n", 1)[0].removeprefix("# Skill: ").strip()
            bare = identity.split("@", 1)[0]
            loaded = [identity if item == bare else item for item in loaded]
        if isinstance(content, str) and content.lstrip().startswith("{"):
            try:
                routed = json.loads(content)
            except json.JSONDecodeError:
                routed = None
            if isinstance(routed, dict) and routed.get("match"):
                identity = routed["match"]
                if routed.get("revision"):
                    identity += f"@{routed['revision']}"
                if identity not in loaded:
                    loaded.append(identity)
        u = getattr(m, "usage_metadata", None)
        if u:
            usage["input_tokens"] += u.get("input_tokens", 0)
            usage["output_tokens"] += u.get("output_tokens", 0)
    final = messages[-1].content if messages else ""
    if isinstance(final, list):  # some models return content blocks, not a plain string
        final = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in final)
    result = (final, loaded, usage)
    return result + (behavior_events(messages),) if include_behavior else result


_AGENT_TOOL_DENYLIST = {"list_skills", "route_and_load"}


async def _connect(retries: int = 20, delay: float = 1.5):
    """LangChain tools for the deep agent (deepagents needs LangChain tools, so this uses
    langchain-mcp-adapters rather than the FastMCP client). Retries while the server is starting."""
    client = MultiServerMCPClient({"skills": {"url": MCP_URL, "transport": "streamable_http"}})
    for i in range(retries):
        try:
            return [tool for tool in await client.get_tools() if tool.name not in _AGENT_TOOL_DENYLIST]
        except Exception as e:  # MCP server may still be starting
            if i == retries - 1:
                raise
            print(f"[agent] waiting for MCP at {MCP_URL} … ({e.__class__.__name__})", flush=True)
            await asyncio.sleep(delay)


async def main(task: str):
    print("=" * 64)
    print(f"TASK: {task}")
    print("=" * 64)

    # 1) Proposed skills — one FastMCP client session; `.data` is the parsed result, no
    # content-block plumbing. route_and_load supplies the routed skill@revision for trace tags.
    from fastmcp import Client
    async with Client(MCP_URL) as client:
        proposals = (await client.call_tool("suggest_skills", {"task": task, "k": 5})).data
        routed = (await client.call_tool(
            "route_and_load", {"task": task, "harness": "claude", "cwd": "/app"})).data
    print("\nPROPOSED SKILLS (MCP suggest_skills):")
    for proposal in proposals:
        tag = " (related — compose/extend)" if proposal.get("related") else ""
        print(f"  {proposal.get('score', 0):>6}  {proposal.get('name')}{tag} — "
              f"{str(proposal.get('description'))[:72]}")

    from optimize import openrouter_key_missing
    if openrouter_key_missing():
        print("\n[agent] OPENROUTER_API_KEY not set — showing router proposals only.")
        print("        Set OPENROUTER_API_KEY in .env to run the deep agent (or point")
        print("        MODEL_BASE_URL at a local vLLM/Ollama endpoint — no key needed).")
        return

    # 2) Deep agent autonomously loads a skill via get_skill and solves. No proposals at all
    # (not even related) means the agent will attempt to propose a new skill, so escalate to the
    # strong model. Persistence is independently controlled by the MCP server's opt-in write flag.
    escalate = not proposals
    if escalate:
        print(f"\nSERVING MODEL: {strong_model()} (strong: no skill matched)")
    else:
        print(f"\nSERVING MODEL: {MODEL}")
    agent = build_agent(await _connect(), strong=escalate)
    # Trace tags: plain skill name feeds mine.py's relevance filter, revision=name@rev pins the
    # exact version served, novel marks strong-model escalations — the convention documented for
    # external harnesses (README: Tracing from your own harness).
    tags = ["demo", "novel"] if escalate else ["demo"]
    if routed.get("match"):
        tags.append(routed["match"])
        if routed.get("revision"):
            tags.append(f"revision={routed['match']}@{routed['revision']}")
    final, loaded, usage = await run_task(agent, task, config=langfuse_config(tags=tags))
    _log_local_trace(task, final, tags)

    print(f"\nLOADED SKILLS (MCP get_skill): {loaded or '(none)'}")
    print(f"TOKENS: {usage['input_tokens']} in / {usage['output_tokens']} out")
    print("\nRESULT:")
    print(final)
    print("=" * 64)

    if langfuse_config():  # same predicate that decided whether tracing was on
        from langfuse import get_client
        get_client().flush()  # one-shot process: make sure the trace ships before exit
        print("[agent] trace sent to Langfuse (http://localhost:3100)")


def _log_local_trace(task: str, answer: str, tags: list[str]) -> None:
    """Append the run to the local JSONL trace store, the zero-infrastructure record that keeps
    optimize-mine working when the Langfuse stack isn't running. Written unconditionally: with
    Langfuse on it doubles as a plain-text backup; failures never break the serving path."""
    import time
    from optimize import traces_file
    try:
        path = traces_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps({"ts": int(time.time()), "task": task,
                                "answer": answer, "tags": tags}) + "\n")
    except OSError as e:
        print(f"[agent] local trace store unavailable ({e}) — run not recorded locally")


if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) or os.environ.get(
        "DEMO_TASK", "How do I merge several PDFs into one and add page numbers?")
    asyncio.run(main(task))
