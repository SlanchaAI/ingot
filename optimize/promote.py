"""Promote a challenger: write the full skill (description + body + bundled files) back to
skills/<name>/, then hot-reload the running MCP server via its reload_skills tool — live, no restart."""
import asyncio
import json
import os
from pathlib import Path

from fastmcp import Client

from mcp_server.registry import SKILLS_DIR, SLUG_RE, write_components

MCP_URL = os.environ.get("MCP_URL", "http://mcp:8000/mcp")
PENDING_DIR = Path(__file__).resolve().parent.parent / "runs" / "pending"


def check_slug(skill: str) -> str:
    # skill names are slugs — anything else is a path-traversal attempt
    if not SLUG_RE.fullmatch(skill):
        raise ValueError(f"invalid skill name: {skill!r}")
    return skill


def pending_path(skill: str) -> Path:
    return PENDING_DIR / f"{check_slug(skill)}.json"


def save_pending(skill: str, data: dict) -> Path:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    p = pending_path(skill)
    p.write_text(json.dumps(data, indent=2))
    return p


def load_pending(skill: str) -> dict | None:
    p = pending_path(skill)
    return json.loads(p.read_text()) if p.exists() else None


async def _reload_mcp() -> str:
    async with Client(MCP_URL) as client:
        return str((await client.call_tool("reload_skills", {})).data)


def promote(skill: str, components: dict[str, str]) -> str:
    """Write the challenger's full skill (description + body + bundled files) into skills/<skill>/,
    hot-reload the MCP server, clear the pending file."""
    skill_dir = SKILLS_DIR / check_slug(skill)
    write_components(skill_dir, components)
    reload_msg = asyncio.run(_reload_mcp())
    pending_path(skill).unlink(missing_ok=True)
    return f"Promoted '{skill}' ({skill_dir}/SKILL.md). MCP: {reload_msg}"
