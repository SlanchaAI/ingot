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

INSTRUCTIONS = """You are a deep agent with access to a read-only skill router over MCP.
For every nontrivial task, call `route_and_load` once with the full task, harness `codex`, current
working directory, and available tool/MCP names. If it returns a match, follow `skill_body`. If it
returns no match, solve the task directly. Never request or inject a skill catalog. Keep the final
answer concise."""


def build_agent(tools, instructions: str = INSTRUCTIONS):
    """The deep agent, wired to the given tools. Reused by the A/B optimizer."""
    from langchain_openai import ChatOpenAI
    from deepagents import create_deep_agent

    model = ChatOpenAI(model=MODEL, base_url=BASE_URL, api_key=API_KEY, temperature=0)
    return create_deep_agent(model=model, tools=tools, system_prompt=instructions)


def langfuse_config(tags: list[str] | None = None) -> dict:
    """ainvoke config with a Langfuse callback if keys are set, else empty."""
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return {}
    from langfuse.langchain import CallbackHandler
    return {"callbacks": [CallbackHandler()], "metadata": {"langfuse_tags": tags or []}}


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
    """Run one task; returns (final answer, skills loaded via get_skill, token usage).
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


async def _connect(retries: int = 20, delay: float = 1.5):
    """LangChain tools for the deep agent (deepagents needs LangChain tools, so this uses
    langchain-mcp-adapters rather than the FastMCP client). Retries while the server is starting."""
    client = MultiServerMCPClient({"skills": {"url": MCP_URL, "transport": "streamable_http"}})
    for i in range(retries):
        try:
            return await client.get_tools()
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
        route = (await client.call_tool("route_and_load", {
            "task": task, "harness": "codex", "cwd": os.getcwd(),
            "available_tools": [], "available_mcps": [],
        })).data
    print("\nROUTE (MCP route_and_load):")
    print(f"  {route.get('score', 0):>6}  {route.get('match') or '(no match)'} — {route.get('reason')}")

    if not API_KEY:
        print("\n[agent] OPENROUTER_API_KEY not set — showing router decision only.")
        print("        Set OPENROUTER_API_KEY in .env to run the deep agent.")
        return

    # 2) Deep agent autonomously loads a skill via get_skill and solves. Traced to Langfuse if configured.
    agent = build_agent(await _connect())
    final, loaded, usage = await run_task(agent, task, config=langfuse_config(tags=["demo"]))

    print(f"\nLOADED SKILLS (MCP route_and_load): {loaded or '(none)'}")
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
