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
    server. Defaults to the offline teacher (the model that authors skills)."""
    return os.environ.get("STRONG_MODEL") or os.environ.get("GEPA_MODEL", "z-ai/glm-5.2")

INSTRUCTIONS = """You are a deep agent. Skill selection has already been performed by the
compatible router. Follow the loaded skill below when one is present. Never call suggestion or
loading tools to replace the router's decision.

If the routing result says this is a novel task, solve it from your own knowledge, then before your
final answer call `create_skill` exactly once to attempt to persist a reusable skill distilled from
your solution (description = one paragraph starting "Use
  this skill when..."; body = the general method/steps, not the specifics of this one request).
  This queues an inactive candidate for human review and must not affect the answer.

{routing_context}

Prefer reusing an existing skill over creating a new one. Keep the final answer concise.
Your final answer must contain the complete deliverable itself, e.g. full runnable code inline,
never just a description of or reference to files you created in your workspace: the user cannot
see your workspace."""


def instructions_for_route(routed: dict) -> str:
    """Render the canonical route result into the serving prompt without re-routing in the model."""
    if routed.get("match"):
        context = (f"# Loaded skill: {routed['match']}\n\n{routed.get('skill_body', '')}")
    elif routed.get("related_match"):
        context = (f"# Loaded related skill: {routed['related_match']}\n\n"
                   "Use these compatible instructions as a starting point. Compose or extend "
                   f"them for the task.\n\n{routed.get('skill_body', '')}")
    elif routed.get("novel"):
        context = "# Routing result\n\nNovel task. No compatible or related skill is available."
    else:
        context = "# Routing result\n\nNo compatible skill body is available."
    return INSTRUCTIONS.format(routing_context=context)


def should_escalate(routed: dict) -> bool:
    """Model selection is derived exclusively from the canonical compatible route."""
    return bool(routed.get("novel"))


def build_agent(tools, instructions: str | None = None, strong: bool = False):
    """The deep agent, wired to the given tools. Reused by the A/B and by candidate rollouts,
    which must stay on the weak MODEL (skills are optimized for the model that serves them),
    only the serving entrypoint passes strong=True, for novel tasks with no skill to load."""
    from langchain_openai import ChatOpenAI
    from deepagents import create_deep_agent

    if strong:
        model = ChatOpenAI(model=strong_model(), temperature=0,
                           **client_kwargs(teacher_base_url(), key=api_key()))
    else:
        model = ChatOpenAI(model=MODEL, temperature=0,
                           **client_kwargs(model_base_url(), key=model_api_key()))
    return create_deep_agent(model=model, tools=tools,
                             system_prompt=instructions or INSTRUCTIONS.format(routing_context=""))


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


_AGENT_TOOL_DENYLIST = {"list_skills", "suggest_skills", "get_skill", "route_and_load"}
_MCP_SERVER_NAMES = ["skills"]


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


def _serving_tools(tools) -> list:
    """Return exactly the connected tools exposed to the serving agent."""
    return [tool for tool in tools if tool.name not in _AGENT_TOOL_DENYLIST]


def _route_result(value) -> dict:
    """Decode the MCP adapter's text content blocks into the router's response object."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        text = "".join(block.get("text", "") for block in value if isinstance(block, dict))
        decoded = json.loads(text)
        if isinstance(decoded, dict):
            return decoded
    raise ValueError("route_and_load returned an invalid response")


async def _route(task: str, tools, available_tools) -> dict:
    """Request the single canonical route used for both prompting and trace attribution."""
    route_tool = next(tool for tool in tools if tool.name == "route_and_load")
    result = await route_tool.ainvoke({
        "task": task,
        "harness": "claude",
        "cwd": "/app",
        "available_tools": sorted({tool.name for tool in available_tools}),
        "available_mcps": _MCP_SERVER_NAMES,
    })
    return _route_result(result)


def _print_route(routed: dict) -> None:
    """Display the selected route and any compatible alternatives."""
    proposals = ([{"name": routed["match"], "score": routed["score"],
                   "description": routed["reason"]}] if routed.get("match") else [])
    if routed.get("related_match"):
        proposals.append({"name": routed["related_match"], "score": routed["score"],
                          "description": routed["reason"], "related": True})
    proposals.extend(routed.get("alternatives", []))
    print("\nCOMPATIBLE ROUTE (MCP route_and_load):")
    for proposal in proposals:
        tag = " (related, compose/extend)" if proposal.get("related") else ""
        explanation = proposal.get("description") or proposal.get("reason")
        print(f"  {proposal.get('score', 0):>6}  {proposal.get('name')}{tag}: "
              f"{str(explanation)[:72]}")


def _route_identity(routed: dict) -> str | None:
    """Return the served skill identity, including its revision when available."""
    selected = routed.get("match") or routed.get("related_match")
    if not selected:
        return None
    revision = routed.get("revision")
    return f"{selected}@{revision}" if revision else selected


def _trace_tags(routed: dict, escalate: bool) -> list[str]:
    """Build trace tags for the selected route and serving model."""
    tags = ["demo", "novel"] if escalate else ["demo"]
    identity = _route_identity(routed)
    if identity:
        tags.append(routed.get("match") or routed["related_match"])
    if routed.get("related_match"):
        tags.append("related")
    if identity and routed.get("revision"):
        tags.append(f"revision={identity}")
    return tags


async def _serve(task: str, routed: dict, tools) -> None:
    """Serve a routed task, record its trace, and print the result."""
    escalate = should_escalate(routed)
    if escalate:
        print(f"\nSERVING MODEL: {strong_model()} (strong: no skill matched)")
    else:
        print(f"\nSERVING MODEL: {MODEL}")
    agent = build_agent(tools, instructions=instructions_for_route(routed), strong=escalate)
    # Trace tags: plain skill name feeds mine.py's relevance filter, revision=name@rev pins the
    # exact version served, novel marks strong-model escalations, the convention documented for
    # external harnesses (README: Tracing from your own harness).
    tags = _trace_tags(routed, escalate)
    final, loaded, usage = await run_task(agent, task, config=langfuse_config(tags=tags))
    identity = _route_identity(routed)
    if identity:
        loaded = [identity]
    _log_local_trace(task, final, tags)

    print(f"\nLOADED SKILLS (MCP route_and_load): {loaded or '(none)'}")
    print(f"TOKENS: {usage['input_tokens']} in / {usage['output_tokens']} out")
    print("\nRESULT:")
    print(final)
    print("=" * 64)

    if langfuse_config():  # same predicate that decided whether tracing was on
        from langfuse import get_client
        get_client().flush()  # one-shot process: make sure the trace ships before exit
        print("[agent] trace sent to Langfuse (http://localhost:3100)")


async def main(task: str):
    print("=" * 64)
    print(f"TASK: {task}")
    print("=" * 64)

    connected_tools = await _connect()
    serving_tools = _serving_tools(connected_tools)
    routed = await _route(task, connected_tools, serving_tools)
    _print_route(routed)

    from optimize import openrouter_key_missing
    if openrouter_key_missing():
        print("\n[agent] OPENROUTER_API_KEY not set, showing router proposals only.")
        print("        Set OPENROUTER_API_KEY in .env to run the deep agent (or point")
        print("        MODEL_BASE_URL at a local vLLM/Ollama endpoint, no key needed).")
        return
    await _serve(task, routed, serving_tools)


def _log_local_trace(task: str, answer: str, tags: list[str]) -> None:
    """Append to the optional local JSONL store without letting trace failures break serving."""
    from agent.traces import write
    try:
        write(task, answer, tags)
    except (OSError, ValueError) as e:
        print(f"[agent] local trace store unavailable ({e}), run not recorded locally")


if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) or os.environ.get(
        "DEMO_TASK", "How do I merge several PDFs into one and add page numbers?")
    asyncio.run(main(task))
