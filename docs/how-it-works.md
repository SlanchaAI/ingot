# How it works

<p align="center">
  <img src="ingot.jpg" alt="Ingot, the mascot, handing skills out to AI agents" width="720">
</p>

- **`mcp_server/`**: [FastMCP](https://github.com/jlowin/fastmcp) v3 server (HTTP transport), five tools:
  - `suggest_skills(task, k)`: routable matches by embedding similarity (Qwen3-Embedding-0.6B
    q4 on CPU ONNX Runtime, no GPU; any fastembed model via `EMBED_MODEL`); near-misses come back flagged
    `related`; empty = truly novel
  - `get_skill(name)`: the full SKILL.md; the header line carries the content-hash revision
  - `list_skills()`: every skill's name, routing description, and load count (`uses`)
  - `reload_skills()`: hot reload after approval or direct operator edits
  - `route_and_load(task, harness, cwd, available_tools, available_mcps)`: one-round-trip
    selection and loading for direct or related compatible routes (see
    [Bring your own agent](mcp-integration.md#bring-your-own-agent-mcp-only))
- **`agent/run.py`**: [deepagents](https://github.com/langchain-ai/deepagents) LangGraph agent
  wired to those tools, traced to Langfuse. Serves routed tasks on the weak `AGENT_MODEL` and
  escalates truly novel tasks to `STRONG_MODEL`.
- **`skills/<name>/SKILL.md`**: YAML `description` is the routing key; the body is what the agent
  loads. Its folder's content hash is its revision.
- **`optimize/promote.py`**: the change-control core, and the only module that writes under
  `skills/`: the pending queue, the evidence check, revision snapshots, the atomic promotion and
  rollback swaps, and the approval-audit append.
- **`optimize/`** (the rest, all optional): trace mining (`mine.py`), multi-dimensional LLM judge
  (`judge.py`), the SkillOpt candidate search (`skillopt_loop.py` + `skillopt_bridge.py`) and its
  rollout/teacher plumbing (`rollout.py`),
  held-out A/B (`ab.py`), the portable evidence bundle (`evidence.py`), the routing pass
  (`routing.py`), the background loop (`loop.py`), the library-wide routing health check
  (`routing_health.py`, embedding-only, cron/CI-friendly, read-only), token ledger (`usage.py`).
  None of these can activate anything: most write pending records; `routing_health.py` writes
  nothing at all. A/B agents get mutation tools stripped. The mining,
  categorized-failure, and failure-diagnosis ideas (plus the minimal-edit author angle) are borrowed
  from [SkillForge (Liu et al., arXiv:2604.08618)](https://arxiv.org/abs/2604.08618).
- **`ui/`**: FastAPI change-control UI (one HTML page, no build step): evidence and the approve /
  reject decision first, then revision history and rollback, then the library and the optional
  candidate runs. It is the only normal application path that activates a pending rewrite.

