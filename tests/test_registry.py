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
    # an optimization pass may only touch description + body, never a license, script, or
    # bundled doc
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
    a, b, local = tmp_path / "a", tmp_path / "b", tmp_path / "local"
    a.mkdir(); b.mkdir(); local.mkdir()
    monkeypatch.setenv("SKILL_ROUTER_PATHS", os.pathsep.join([str(a), str(b), str(a)]))
    monkeypatch.setattr("mcp_server.registry.SKILLS_DIR", local)
    assert configured_roots() == [local.resolve(), a.resolve(), b.resolve()]


def test_explicit_roots_override_environment(tmp_path, monkeypatch):
    env_root, explicit, local = tmp_path / "env", tmp_path / "explicit", tmp_path / "local"
    env_root.mkdir(); explicit.mkdir(); local.mkdir()
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(env_root))
    monkeypatch.setattr("mcp_server.registry.SKILLS_DIR", local)
    assert configured_roots([explicit]) == [local.resolve(), explicit.resolve()]


def test_environment_roots_keep_local_authoring_root(tmp_path, monkeypatch):
    external, local = tmp_path / "external", tmp_path / "local"
    external.mkdir(); local.mkdir()
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(external))
    monkeypatch.setattr("mcp_server.registry.SKILLS_DIR", local)
    assert configured_roots() == [local.resolve(), external.resolve()]


def test_load_skills_uses_declared_root_precedence_with_warning(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _skill(a, body="first"); _skill(b, dirname="other", name="sample", body="second")
    with pytest.warns(UserWarning, match="duplicate skill 'sample'"):
        skills = load_skills(roots=[a, b])
    assert len(skills) == 1 and skills[0].body == "first"


def test_skill_revision_changes_with_routing_content(tmp_path):
    d = _skill(tmp_path)
    first = load_skills(tmp_path)[0]
    (d / "SKILL.md").write_text("---\nname: sample\ndescription: changed route\n---\nbody\n")
    second = load_skills(tmp_path)[0]
    assert len(first.revision) == 64
    assert first.revision != second.revision
    assert first.root == str(d.resolve())


def test_skill_revision_changes_with_bundled_content(tmp_path):
    d = _skill(tmp_path)
    (d / "reference.md").write_text("version one")
    first = load_skills(tmp_path)[0]
    (d / "reference.md").write_text("version two")
    second = load_skills(tmp_path)[0]
    assert first.revision != second.revision


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


def test_router_metadata_rejects_scalar_for_list_field(tmp_path):
    _skill(tmp_path, extra="metadata:\n  skill-router:\n    harnesses: codex\n")
    with pytest.raises(ValueError, match="harnesses.*list"):
        load_skills(tmp_path)


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


# --- hidden staging directories ---------------------------------------------------------------

def _hidden_stage(root, name, body, suffix="stage"):
    """What promotion and rollback leave behind when they are killed mid-swap."""
    stage = root / f".{name}.deadbeef.{suffix}"
    stage.mkdir(parents=True)
    write_skill_md(stage / "SKILL.md", {"name": name, "description": "shadow trigger"}, body)
    return stage


def _live_skill(root, name="pdf", body="approved body"):
    live = root / name
    live.mkdir(parents=True)
    write_skill_md(live / "SKILL.md", {"name": name, "description": "Merge PDFs."}, body)
    return live


def test_leftover_staging_directory_cannot_shadow_the_live_skill(tmp_path):
    """Path.glob matches hidden names and '.' sorts ahead of every slug, so an abandoned
    `.pdf.<hex>.stage` would win the duplicate-name check and serve its body instead of the
    approved one."""
    _live_skill(tmp_path)
    _hidden_stage(tmp_path, "pdf", "abandoned body")

    skills = load_skills(tmp_path)

    assert [s.name for s in skills] == ["pdf"]
    assert skills[0].body == "approved body"
    assert skills[0].description == "Merge PDFs."
    assert "/.pdf." not in skills[0].path


def test_leftover_staging_directory_is_not_published_as_its_own_skill(tmp_path):
    """With no live skill of that name the staging copy must still not be served."""
    _hidden_stage(tmp_path, "pdf", "abandoned body")
    assert load_skills(tmp_path) == []


@pytest.mark.parametrize("suffix", ["stage", "previous", "rollback"])
def test_skill_sources_skips_every_staging_suffix(tmp_path, suffix):
    from mcp_server.registry import skill_sources
    _live_skill(tmp_path)
    _hidden_stage(tmp_path, "pdf", "abandoned body", suffix=suffix)
    assert [p.parent.name for p in skill_sources(tmp_path)] == ["pdf"]
