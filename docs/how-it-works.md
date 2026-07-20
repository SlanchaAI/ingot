# How it works

<p align="center">
  <img src="docs/ingot.jpg" alt="Ingot, the mascot, handing skills out to AI agents" width="720">
</p>

- **`mcp_server/`**: [FastMCP](https://github.com/jlowin/fastmcp) v3 server (HTTP transport), six tools:
  - `suggest_skills(task, k)`: routable matches by embedding similarity (CPU
    [fastembed](https://github.com/qdrant/fastembed), no GPU); near-misses come back flagged
    `related`; empty = truly novel
  - `get_skill(name)`: the full SKILL.md; the header line carries the content-hash revision
  - `list_skills()`: every skill's name, routing description, and load count (`uses`)
  - `create_skill(name, description, body)`: queue a new agent-authored candidate (never activates
    or overwrites)
  - `reload_skills()`: hot reload after approval or direct operator edits
  - `route_and_load(task, harness, cwd, available_tools, available_mcps)`: one-round-trip
    selection and loading for direct or related compatible routes (see
    [Bring your own agent](mcp-integration.md#bring-your-own-agent-mcp-only))
- **`agent/run.py`**: [deepagents](https://github.com/langchain-ai/deepagents) LangGraph agent
  wired to those tools, traced to Langfuse. Serves routed tasks on the weak `AGENT_MODEL` and
  escalates truly novel tasks to `STRONG_MODEL`, which can queue reusable candidates for review.
- **`skills/<name>/SKILL.md`**: YAML `description` is the routing key; the body is what the agent
  loads. Its folder's content hash is its revision.
- **`optimize/promote.py`**: the change-control core, and the only module that writes under
  `skills/`: the pending queue, the evidence check, revision snapshots, the atomic promotion and
  rollback swaps, and the approval-audit append.
- **`optimize/`** (the rest, all optional): trace mining (`mine.py`), multi-dimensional LLM judge
  (`judge.py`), the SkillOpt candidate search (`skillopt_loop.py` + `skillopt_bridge.py`) and its
  rollout/teacher plumbing (`rollout.py`),
  held-out A/B (`ab.py`), the portable evidence bundle (`evidence.py`), the routing pass
  (`routing.py`), the background loop (`loop.py`), token ledger (`usage.py`). None of these can
  activate anything: they write pending records. A/B agents get mutation tools stripped. The mining,
  categorized-failure, and failure-diagnosis ideas (plus the minimal-edit author angle) are borrowed
  from [SkillForge (Liu et al., arXiv:2604.08618)](https://arxiv.org/abs/2604.08618).
- **`ui/`**: FastAPI change-control UI (one HTML page, no build step): evidence and the approve /
  reject decision first, then revision history and rollback, then the library and the optional
  candidate runs. It is the only normal application path that activates a pending creation or
  rewrite.

