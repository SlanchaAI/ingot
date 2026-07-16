"""LangGraph deep agent that uses the MCP skill router: it asks the router for skill suggestions,
loads the best skill's instructions, and follows them to solve the task. Prints the whole flow:
task -> proposed skills -> loaded skills -> result.

Env: OPENROUTER_API_KEY (required for the agent), MODEL, MCP_URL,
LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL (optional tracing).
"""
import os
import sys
import asyncio
import json
import hashlib

from langchain_mcp_adapters.client import MultiServerMCPClient

MCP_URL = os.environ.get("MCP_URL", "http://mcp:8000/mcp")
MODEL = os.environ.get("MODEL", "qwen/qwen3.6-27b")
BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
# Zero data retention, hardcoded: every OpenRouter call routes only to endpoints that neither
# retain prompts (zdr) nor collect user data (README: Privacy). Same literal in optimize/judge.py
# (kept in sync by a test) so the optimizer doesn't import this module's heavy deps.
ZDR_PROVIDER = {"provider": {"zdr": True, "data_collection": "deny"}}

INSTRUCTIONS = """You are a deep agent with access to a skill router over MCP.
For every task, first call `suggest_skills`, then decide from what it returns. Only ever call
`get_skill` with a name that `suggest_skills` returned — never guess skill names.

- If it returns a **direct match** (entries with no `related` flag): call `get_skill` on the top one,
  read it, and follow it.
- If it returns only **`related: true`** entries (no direct match): load the closest with `get_skill`
  and *compose or extend* it into your solution rather than authoring a duplicate.
- If it returns an **empty list** (nothing even related — a truly novel task): solve it from your own
  knowledge, then before your final answer you MUST call `create_skill` exactly once to persist a
  reusable skill distilled from your solution (description = one paragraph starting "Use this skill
  when..."; body = the general method/steps, not the specifics of this one request).

Prefer reusing/extending an existing skill over creating a new one. Keep the final answer concise."""


def build_agent(tools, instructions: str = INSTRUCTIONS):
    """The deep agent, wired to the given tools. Reused by the A/B optimizer."""
    from langchain_openai import ChatOpenAI
    from deepagents import create_deep_agent

    model = ChatOpenAI(model=MODEL, base_url=BASE_URL, api_key=API_KEY, temperature=0,
                       extra_body=ZDR_PROVIDER)
    return create_deep_agent(model=model, tools=tools, system_prompt=instructions)


def langfuse_config(tags: list[str] | None = None, trace_id: str | None = None) -> dict:
    """ainvoke config with a Langfuse callback if keys are set, else empty. Pass `trace_id`
    (from Langfuse.create_trace_id()) to pin the run to a known trace so callers can attach
    scores to it afterwards (the canary does this per request)."""
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

    # 1) Proposed skills — one FastMCP client call; `.data` is the parsed list, no content-block plumbing.
    from fastmcp import Client
    async with Client(MCP_URL) as client:
        proposals = (await client.call_tool("suggest_skills", {"task": task, "k": 5})).data
    print("\nPROPOSED SKILLS (MCP suggest_skills):")
    for proposal in proposals:
        tag = " (related — compose/extend)" if proposal.get("related") else ""
        print(f"  {proposal.get('score', 0):>6}  {proposal.get('name')}{tag} — "
              f"{str(proposal.get('description'))[:72]}")

    if not API_KEY:
        print("\n[agent] OPENROUTER_API_KEY not set — showing router proposals only.")
        print("        Set OPENROUTER_API_KEY in .env to run the deep agent.")
        return

    # 2) Deep agent autonomously loads a skill via get_skill and solves.
    agent = build_agent(await _connect())
    final, loaded, usage = await run_task(agent, task, config=langfuse_config(tags=["demo"]))

    print(f"\nLOADED SKILLS (MCP get_skill): {loaded or '(none)'}")
    print(f"TOKENS: {usage['input_tokens']} in / {usage['output_tokens']} out")
    print("\nRESULT:")
    print(final)
    print("=" * 64)

    if langfuse_config():  # same predicate that decided whether tracing was on
        from langfuse import get_client
        get_client().flush()  # one-shot process: make sure the trace ships before exit
        print("[agent] trace sent to Langfuse (http://localhost:3100)")


if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) or os.environ.get(
        "DEMO_TASK", "How do I merge several PDFs into one and add page numbers?")
    asyncio.run(main(task))
