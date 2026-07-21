"""MCP server for discovering, loading, and improving Agent Skills."""
from __future__ import annotations

import os
import threading

from fastmcp import FastMCP

from . import usage_counts
from .registry import configured_roots, load_skills
from .router import Router

MIN_SCORE = float(os.environ.get("MIN_SCORE", "0.65"))
RELATED_SCORE = float(os.environ.get("RELATED_SCORE", "0.45"))
PORT = int(os.environ.get("PORT", "8000"))
# Loopback by default: the tools are unauthenticated, so a bare `python -m mcp_server.server` must
# not listen on the network. The compose mcp service sets HOST=0.0.0.0 (required for Docker port
# publishing); host access stays localhost-only via the 127.0.0.1 port mapping.
HOST = os.environ.get("HOST", "127.0.0.1")


class _State:
    def __init__(self):
        self._lock = threading.RLock()

    @staticmethod
    def _signature(roots) -> tuple:
        """Change detector over exactly the directories load_skills reads. Hidden directories are
        excluded there (promotion/rollback staging), so watching them here would only churn."""
        files = []
        for root in roots:
            for skill_root in root.iterdir() if root.exists() else ():
                if skill_root.is_dir() and not skill_root.name.startswith("."):
                    files.extend(path for path in skill_root.rglob("*") if path.is_file())
        return tuple((str(path.resolve()), path.stat().st_mtime_ns, path.stat().st_size)
                     for path in sorted(files))

    def reload(self, roots=None) -> int:
        selected_roots = configured_roots(roots)
        skills = load_skills(roots=selected_roots)
        router = Router(skills)
        signature = self._signature(selected_roots)
        with self._lock:
            self.roots, self.skills, self.router = selected_roots, skills, router
            self.by_name = {skill.name: skill for skill in skills}
            self.signature = signature
            return len(self.skills)

    def refresh_if_changed(self) -> None:
        with self._lock:
            roots, prior = list(self.roots), self.signature
        if self._signature(roots) != prior:
            self.reload(roots)


STATE = _State()
STATE.reload()
mcp = FastMCP("ingot")


@mcp.tool()
def list_skills() -> list[dict]:
    """List all available skills by name, routing description, and load count (times served)."""
    STATE.refresh_if_changed()
    counts = usage_counts.load_counts()
    return [{"name": skill.name, "description": skill.description,
             "uses": counts.get(skill.name, 0)} for skill in STATE.skills]


@mcp.tool()
def suggest_skills(task: str, k: int = 5) -> list[dict]:
    """Suggest routable or related skills for a task, ranked by similarity."""
    STATE.refresh_if_changed()
    matched = STATE.router.suggest(task, k, min_score=MIN_SCORE)
    if matched:
        return matched
    related = STATE.router.suggest(task, k=2, min_score=RELATED_SCORE)
    for candidate in related:
        candidate["related"] = True
    return related


@mcp.tool()
def get_skill(name: str) -> str:
    """Load one skill's instructions by exact name. The header line carries the content-hash
    revision (`# Skill: <name>@<revision>`) so harnesses can attribute traces to the exact
    skill version they served."""
    STATE.refresh_if_changed()
    skill = STATE.by_name.get(name)
    if not skill:
        return f"No skill named '{name}'. Use suggest_skills or list_skills first."
    usage_counts.record_use(skill.name)
    identity = f"{skill.name}@{skill.revision}" if skill.revision else skill.name
    return f"# Skill: {identity}\n{skill.description}\n\n{skill.body}"


@mcp.tool()
def reload_skills() -> str:
    """Re-read skill roots and rebuild the router after a promotion."""
    count = STATE.reload()
    print(f"[ingot] reloaded: {count} skills", flush=True)
    return f"Reloaded {count} skills."


@mcp.tool()
def route_and_load(task: str, harness: str, cwd: str, available_tools: list[str] | None = None,
                   available_mcps: list[str] | None = None) -> dict:
    """Select one compatible skill for a task and return its instructions, or return no match.
    The result's `novel` flag is the weak/strong routing signal for the calling harness:
    a direct `match` -> follow `skill_body`; a `related_match` with `novel` false -> use its loaded
    body to compose or extend; `novel` true -> nothing even related, so serve with your strong
    model. Only the selected compatible match can carry a body; alternatives are metadata-only."""
    STATE.refresh_if_changed()
    result = STATE.router.route(task, harness, cwd, available_tools or [], available_mcps or [],
                                min_score=MIN_SCORE, related_score=RELATED_SCORE)
    if result.get("match"):
        usage_counts.record_use(result["match"])
    return result


if __name__ == "__main__":
    print(f"[ingot] {len(STATE.skills)} skills loaded; serving MCP on :{PORT}/mcp", flush=True)
    mcp.run(transport="http", host=HOST, port=PORT, path="/mcp",
            allowed_hosts=["*"], allowed_origins=["*"])
