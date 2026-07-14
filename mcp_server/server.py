"""Minimal MCP skill-router server ([FastMCP](https://github.com/jlowin/fastmcp) v3, HTTP transport).
Exposes five tools so an agent can discover, get suggestions for, load, create, and refresh skills:
  - list_skills()             -> every skill's name + description
  - suggest_skills(task, k)   -> top-k skills above MIN_SCORE (empty list = no good match)
  - get_skill(name)           -> the full SKILL.md body to load into the agent's context
  - create_skill(...)         -> persist a new skill the agent authored (never overwrites)
  - reload_skills()           -> re-read skills/ from disk (called after a skill is promoted)
"""
import os

from fastmcp import FastMCP

from . import guard_model, safety
from .registry import SKILLS_DIR, load_skills, name_problem, slugify, write_skill_md
from .router import Router

# at/above this similarity a skill is a routable match; suggest_skills loads it
MIN_SCORE = float(os.environ.get("MIN_SCORE", "0.65"))
# no routable match, but at/above this a skill is *related* — surfaced so the agent can compose /
# extend it instead of authoring a near-duplicate (compose-awareness). Below this = truly novel.
RELATED_SCORE = float(os.environ.get("RELATED_SCORE", "0.45"))
# above this, a new skill's description near-duplicates an existing one → reject (route-shadowing)
COLLISION_SCORE = float(os.environ.get("COLLISION_SCORE", "0.93"))
PORT = int(os.environ.get("PORT", "8000"))


class _State:
    """Mutable holder so reload_skills() can swap the registry + router in place."""
    def reload(self) -> int:
        self.skills = load_skills()
        self.router = Router(self.skills)
        self.by_name = {s.name: s for s in self.skills}
        return len(self.skills)


STATE = _State()
STATE.reload()

mcp = FastMCP("skill-router")


@mcp.tool()
def list_skills() -> list[dict]:
    """List all available skills (name + one-line description)."""
    return [{"name": s.name, "description": s.description} for s in STATE.skills]


@mcp.tool()
def suggest_skills(task: str, k: int = 5) -> list[dict]:
    """Suggest skills for a task, ranked by similarity. Returns routable matches (load the top one).
    If none are routable but some are *related*, returns those flagged `related: true` — load the
    closest and compose/extend it rather than authoring a duplicate. Empty means truly novel: solve
    it yourself and consider create_skill."""
    matched = STATE.router.suggest(task, k, min_score=MIN_SCORE)
    if matched:
        return matched
    related = STATE.router.suggest(task, k=2, min_score=RELATED_SCORE)
    for r in related:
        r["related"] = True
    return related


@mcp.tool()
def get_skill(name: str) -> str:
    """Load a skill by name: returns its full SKILL.md instructions to follow."""
    s = STATE.by_name.get(name)
    if not s:
        return f"No skill named '{name}'. Use suggest_skills or list_skills first."
    return f"# Skill: {s.name}\n{s.description}\n\n{s.body}"


@mcp.tool()
def create_skill(name: str, description: str, body: str) -> str:
    """Persist a NEW skill (only when no existing skill covered the task). `description` is the
    routing key: one paragraph starting 'Use this skill when...'. `body` is the SKILL.md content:
    the reusable instructions/steps/code patterns that solve this kind of task."""
    slug = slugify(name)
    problem = name_problem(slug)
    if problem:
        return f"Invalid skill name '{name}': {problem}."
    if slug in STATE.by_name or (SKILLS_DIR / slug).exists():
        return f"Skill '{slug}' already exists — improve it via the optimizer instead."
    # content guardrails (this path writes a live, routable skill with no human approval):
    # fast regex/heuristic scan, then the optional ML prompt-injection classifier if enabled
    problems = safety.scan(description, body)
    ml_flag = guard_model.check(f"{description}\n{body}")
    if ml_flag:
        problems.append(ml_flag)
    if problems:
        return f"Skill '{slug}' rejected: {'; '.join(problems)}."
    shadowed, score = STATE.router.nearest(description)
    if score >= COLLISION_SCORE:
        return (f"Skill '{slug}' rejected: description too similar to existing skill "
                f"'{shadowed}' (cosine {score:.2f}) — would shadow its routing. Refine it or "
                f"improve '{shadowed}' via the optimizer instead.")
    d = SKILLS_DIR / slug
    d.mkdir(parents=True)
    # tag provenance so agent-authored skills are distinguishable from curated / third-party ones
    write_skill_md(d / "SKILL.md", {"name": slug, "description": description, "source": "agent"}, body)
    n = STATE.reload()
    print(f"[skill-router] created skill '{slug}' ({n} skills total)", flush=True)
    return f"Created skill '{slug}' and reloaded the router ({n} skills)."


@mcp.tool()
def reload_skills() -> str:
    """Re-read all skills from disk and rebuild the router (hot reload after a promotion)."""
    n = STATE.reload()
    print(f"[skill-router] reloaded: {n} skills", flush=True)
    return f"Reloaded {n} skills."


if __name__ == "__main__":
    print(f"[skill-router] {len(STATE.skills)} skills loaded; serving MCP on :{PORT}/mcp", flush=True)
    # FastMCP v3's DNS-rebinding protection blocks the container's Host header (mcp:8000) by
    # default — allow the compose service host. This runs on a private docker network / localhost.
    mcp.run(transport="http", host="0.0.0.0", port=PORT, path="/mcp",
            allowed_hosts=["*"], allowed_origins=["*"])
