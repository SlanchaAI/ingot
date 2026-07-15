import asyncio

from mcp_server.server import STATE, mcp, route_and_load


def test_route_and_load_is_additive_to_existing_mcp_tools():
    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == {
        "list_skills", "suggest_skills", "get_skill", "create_skill", "reload_skills",
        "route_and_load",
    }


def test_route_refreshes_after_external_skill_promotion(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    skill = root / "pdf"
    skill.mkdir(parents=True)
    md = skill / "SKILL.md"
    md.write_text("---\nname: pdf\ndescription: Merge PDF files.\n---\nbody one\n")
    STATE.reload([root])
    monkeypatch.setattr("mcp_server.server.MIN_SCORE", 0.0)
    first = route_and_load("merge PDF", "codex", str(tmp_path))
    md.write_text("---\nname: pdf\ndescription: Merge PDF files.\n---\nbody two\n")
    second = route_and_load("merge PDF", "codex", str(tmp_path))
    assert first["skill_body"] == "body one"
    assert second["skill_body"] == "body two"
    assert first["revision"] != second["revision"]


def test_route_refreshes_revision_after_bundled_file_change(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    skill = root / "pdf"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: pdf\ndescription: Merge PDF files.\n---\nbody\n")
    reference = skill / "reference.md"
    reference.write_text("version one")
    STATE.reload([root])
    monkeypatch.setattr("mcp_server.server.MIN_SCORE", 0.0)
    first = route_and_load("merge PDF", "codex", str(tmp_path))
    reference.write_text("version two")
    second = route_and_load("merge PDF", "codex", str(tmp_path))
    assert first["revision"] != second["revision"]
