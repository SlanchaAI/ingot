"""Unit tests for skill discovery / frontmatter parsing edge cases (mcp_server.registry)."""
import pytest

from mcp_server.registry import (
    MAX_NAME, load_skills, name_problem, optimizable_components, parse_skill, slugify, write_skill_md,
)


def test_optimizable_components_excludes_files_and_license(tmp_path):
    d = tmp_path / "skill"
    (d / "scripts").mkdir(parents=True)
    write_skill_md(d / "SKILL.md", {"name": "skill", "description": "route me"}, "the body")
    (d / "reference.md").write_text("docs")
    (d / "LICENSE.txt").write_text("license text")
    (d / "scripts" / "run.py").write_text("print(1)")
    comps = optimizable_components(d)
    # GEPA may only touch description + body — never a license, script, or bundled doc
    assert set(comps) == {"description", "body"}
    assert comps["body"] == "the body" and comps["description"] == "route me"


# --- parse_skill: frontmatter edge cases ------------------------------------------------------

def test_parse_skill_no_frontmatter_uses_fallback_name():
    meta, body = parse_skill("just a body, no frontmatter", "fallback")
    assert meta["name"] == "fallback" and meta["description"] == "" and body == "just a body, no frontmatter"


def test_parse_skill_malformed_yaml_degrades_gracefully():
    meta, body = parse_skill("---\nname: [unclosed\ndescription: x\n---\nbody", "fb")
    assert meta["name"] == "fb" and meta["description"] == ""       # bad YAML -> empty meta -> fallbacks


def test_parse_skill_non_dict_frontmatter_degrades():
    meta, _ = parse_skill("---\n- just\n- a list\n---\nbody", "fb")
    assert meta["name"] == "fb"


def test_parse_skill_missing_name_falls_back_to_dir_name():
    meta, body = parse_skill("---\ndescription: does things\n---\nthe body", "my-skill")
    assert meta["name"] == "my-skill" and meta["description"] == "does things" and body == "the body"


def test_parse_skill_preserves_extra_frontmatter_fields():
    meta, _ = parse_skill("---\nname: s\ndescription: d\nlicense: MIT\nsource: agent\n---\nb", "s")
    assert meta["license"] == "MIT" and meta["source"] == "agent"


# --- load_skills: only skills with a routing description are usable ----------------------------

def test_load_skills_skips_missing_description(tmp_path):
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "SKILL.md").write_text("---\nname: good\ndescription: routable\n---\nbody")
    (tmp_path / "nodesc").mkdir()
    (tmp_path / "nodesc" / "SKILL.md").write_text("---\nname: nodesc\n---\nbody")   # no description
    names = {s.name for s in load_skills(tmp_path)}
    assert names == {"good"}


# --- name_problem / slugify: spec boundaries --------------------------------------------------

def test_name_length_boundary():
    assert name_problem("a" * MAX_NAME) is None          # exactly 64 is allowed
    assert name_problem("a" * (MAX_NAME + 1)) is not None # 65 is not


def test_reserved_word_as_substring_is_rejected():
    assert name_problem("my-claude-tool") is not None
    assert name_problem("anthropicskills") is not None


@pytest.mark.parametrize("raw,expected", [
    ("Merge PDFs!", "merge-pdfs"),
    ("  spaced  out  ", "spaced-out"),
    ("UPPER_case", "upper-case"),
    ("a//b__c", "a-b-c"),
])
def test_slugify_normalizes(raw, expected):
    assert slugify(raw) == expected
