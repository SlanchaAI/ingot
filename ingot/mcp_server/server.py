"""MCP server for discovering, loading, and improving Agent Skills."""
from __future__ import annotations

import os
import threading

from fastmcp import FastMCP

from . import usage_counts
from .registry import configured_roots, load_skills
from .router import Router

MIN_SCORE = float(os.environ.get("MIN_SCORE", "0.53"))
RELATED_SCORE = float(os.environ.get("RELATED_SCORE", "0.37"))
PORT = int(os.environ.get("PORT", "8000"))
# Loopback by default: the tools are unauthenticated, so a bare `python -m ingot.mcp_server.server` must
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


@mcp.tool()
def propose_skill_update(
    skill: str,
    champion_revision: str,
    challenger_body: str,
    summary: str,
    trigger: str,
    minimal_content: str,
    producer: str,
    caller: str,
    evidence: list[str],
    pressure_scenario: str,
    risk: str,
    verification_status: str,
    verification_command: str,
    verification_result: str,
    challenger_description: str = "",
) -> dict:
    """Quarantine an evidence-backed update proposed by skill-retrospective.

    This tool never activates, rejects, or replaces instructions. It accepts one full candidate
    for an existing skill after pressure verification passes, binds it to the exact loaded champion
    revision, and refuses an occupied review slot. Human approval in the Ingot console remains
    required.
    """
    from ingot.optimize.retrospective import submit_skill_update
    return submit_skill_update(
        skill=skill,
        champion_revision=champion_revision,
        challenger_body=challenger_body,
        challenger_description=challenger_description,
        summary=summary,
        trigger=trigger,
        minimal_content=minimal_content,
        producer=producer,
        caller=caller,
        evidence=evidence,
        pressure_scenario=pressure_scenario,
        risk=risk,
        verification_status=verification_status,
        verification_command=verification_command,
        verification_result=verification_result,
    )


@mcp.tool()
def propose_skill_create(
    skill: str,
    description: str,
    body: str,
    files: dict[str, str],
    frontmatter: dict,
    summary: str,
    source: str,
    producer: str,
    caller: str,
    evidence: list[str],
    pressure_scenario: str,
    risk: str,
    verification_status: str,
    verification_command: str,
    verification_result: str,
) -> dict:
    """Quarantine a vetted new skill package as “to be added”; never activate it."""
    from ingot.optimize.ingress import submit_skill_create
    return submit_skill_create(
        skill=skill, description=description, body=body, files=files, frontmatter=frontmatter,
        summary=summary,
        source=source, producer=producer, caller=caller, evidence=evidence,
        pressure_scenario=pressure_scenario, risk=risk,
        verification_status=verification_status, verification_command=verification_command,
        verification_result=verification_result,
    )


if __name__ == "__main__":
    print(f"[ingot] {len(STATE.skills)} skills loaded; serving MCP on :{PORT}/mcp", flush=True)
    mcp.run(transport="http", host=HOST, port=PORT, path="/mcp",
            allowed_hosts=["*"], allowed_origins=["*"])
