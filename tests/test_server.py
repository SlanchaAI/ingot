import asyncio

from mcp_server.server import STATE, get_skill, mcp, route_and_load


def test_get_skill_header_carries_revision(tmp_path):
    root = tmp_path / "skills"
    skill = root / "pdf"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: pdf\ndescription: Merge PDF files.\n---\nbody\n")
    STATE.reload([root])
    loaded = get_skill("pdf")
    header = loaded.split("\n", 1)[0]
    assert header.startswith("# Skill: pdf@") and len(header) > len("# Skill: pdf@")
    assert "No skill named" in get_skill("nope")


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


def test_route_and_load_novel_flag_uses_server_thresholds(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    skill = root / "pdf"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: pdf\ndescription: Merge PDF files.\n---\nbody\n")
    STATE.reload([root])
    # match -> weak model serves the skill
    monkeypatch.setattr("mcp_server.server.MIN_SCORE", 0.0)
    assert route_and_load("merge PDF", "codex", str(tmp_path))["novel"] is False
    # no match but within the related band -> compose/extend, still not novel
    monkeypatch.setattr("mcp_server.server.MIN_SCORE", 0.99)
    monkeypatch.setattr("mcp_server.server.RELATED_SCORE", 0.0)
    related = route_and_load("merge PDF", "codex", str(tmp_path))
    assert related["match"] is None and related["novel"] is False
    # nothing even related -> the harness should escalate to its strong model
    monkeypatch.setattr("mcp_server.server.RELATED_SCORE", 0.99)
    novel = route_and_load("merge PDF", "codex", str(tmp_path))
    assert novel["match"] is None and novel["novel"] is True


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
