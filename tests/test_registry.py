"""Unit tests for skill discovery / frontmatter parsing edge cases (mcp_server.registry)."""
import os

import pytest

from mcp_server.registry import (
    MAX_NAME, configured_roots, load_skills, name_problem, optimizable_components, parse_skill,
    slugify, write_skill_md,
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


def _skill(root, dirname="sample", *, name="sample", description="route sample", extra="", body="body"):
    d = root / dirname
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}\n"
    )
    return d


def test_configured_roots_reads_platform_path_separator(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    monkeypatch.setenv("SKILL_ROUTER_PATHS", os.pathsep.join([str(a), str(b), str(a)]))
    assert configured_roots() == [a.resolve(), b.resolve()]


def test_explicit_roots_override_environment(tmp_path, monkeypatch):
    env_root, explicit = tmp_path / "env", tmp_path / "explicit"
    env_root.mkdir(); explicit.mkdir()
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(env_root))
    assert configured_roots([explicit]) == [explicit.resolve()]


def test_load_skills_rejects_duplicate_identity_across_roots(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _skill(a); _skill(b, dirname="other", name="sample")
    with pytest.raises(ValueError, match="duplicate skill 'sample'"):
        load_skills(roots=[a, b])


def test_skill_revision_changes_with_routing_content(tmp_path):
    d = _skill(tmp_path)
    first = load_skills(tmp_path)[0]
    (d / "SKILL.md").write_text("---\nname: sample\ndescription: changed route\n---\nbody\n")
    second = load_skills(tmp_path)[0]
    assert len(first.revision) == 64
    assert first.revision != second.revision
    assert first.root == str(d.resolve())


def test_router_metadata_defaults_and_namespaced_overrides(tmp_path):
    _skill(tmp_path, dirname="default", name="default")
    _skill(
        tmp_path,
        dirname="codex-only",
        name="codex-only",
        extra=("metadata:\n  skill-router:\n    harnesses: [codex]\n    trust: reviewed\n"
               "    activation: manual\n    priority: 90\n"),
    )
    by_name = {s.name: s for s in load_skills(tmp_path)}
    assert by_name["default"].metadata["harnesses"] == ["claude", "codex"]
    assert by_name["default"].metadata["activation"] == "automatic"
    assert by_name["codex-only"].metadata["harnesses"] == ["codex"]
    assert by_name["codex-only"].metadata["trust"] == "reviewed"
    assert by_name["codex-only"].metadata["priority"] == 90


def test_harness_variant_replaces_only_body(tmp_path):
    d = _skill(tmp_path)
    (d / "variants").mkdir()
    (d / "variants" / "codex.md").write_text("codex body")
    skill = load_skills(tmp_path)[0]
    assert skill.body_for("claude") == "body"
    assert skill.body_for("codex") == "codex body"


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
