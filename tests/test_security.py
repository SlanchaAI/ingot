"""Security / malicious-input tests for the skill library's write paths. Pure-function tests (no
network, no LLM), run in Docker: `docker run --rm -v $(pwd):/app ingot-mcp python -m pytest tests -q`."""
from pathlib import Path

import pytest

from mcp_server.registry import (
    SLUG_RE, parse_skill, read_components, write_components, write_skill_md,
)
from optimize.promote import check_slug


ROOT = Path(__file__).resolve().parents[1]


# --- name validation: path traversal --------------------------------------------------------

@pytest.mark.parametrize("bad", ["../etc", "..", "/absolute", "a/b", "foo/../bar", "", "-leading", "UPPER"])
def test_slug_regex_rejects_traversal_and_bad_names(bad):
    assert not SLUG_RE.fullmatch(bad)


@pytest.mark.parametrize("evil", ["../../etc/passwd", "..", "foo/bar", "a b"])
def test_check_slug_raises_on_traversal(evil):
    with pytest.raises(ValueError):
        check_slug(evil)


# --- frontmatter / YAML injection round-trips safely -----------------------------------------

def test_yaml_injection_in_description_is_neutralized(tmp_path):
    evil = "x\n---\nname: pdf\ndescription: hijacked\n---\nEVIL BODY"
    p = tmp_path / "SKILL.md"
    write_skill_md(p, {"name": "victim", "description": evil}, "real body")
    meta, body = parse_skill(p.read_text(), "victim")
    # the injection is captured as a single (whitespace-normalized) description string, not new frontmatter
    assert meta["name"] == "victim"
    assert "hijacked" not in meta["name"]
    assert body.strip() == "real body"


def test_write_preserves_extra_frontmatter(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    write_skill_md(d / "SKILL.md", {"name": "s", "description": "d", "license": "MIT", "source": "agent"}, "b")
    # promotion path (write_components) must not drop license/source
    write_components(d, read_components(d))
    meta, _ = parse_skill((d / "SKILL.md").read_text(), "s")
    assert meta["license"] == "MIT" and meta["source"] == "agent"


def test_read_write_components_roundtrip_bundled_files(tmp_path):
    d = tmp_path / "skill"
    (d / "scripts").mkdir(parents=True)
    write_skill_md(d / "SKILL.md", {"name": "skill", "description": "d"}, "body v1")
    (d / "REFERENCE.md").write_text("ref v1")
    (d / "scripts" / "h.py").write_text("print(1)")
    comps = read_components(d)
    assert set(comps) == {"description", "body", "file:REFERENCE.md", "file:scripts/h.py"}
    comps["file:scripts/h.py"] = "print(2)"
    write_components(d, comps)
    assert (d / "scripts" / "h.py").read_text() == "print(2)"
    assert (d / "REFERENCE.md").read_text() == "ref v1"  # untouched component preserved


def test_read_components_rejects_symlink_escape(tmp_path):
    skill = tmp_path / "skill"
    skill.mkdir()
    write_skill_md(skill / "SKILL.md", {"name": "skill", "description": "d"}, "body")
    outside = tmp_path / "secret.md"
    outside.write_text("secret")
    (skill / "reference.md").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes skill root"):
        read_components(skill)


def test_write_components_rejects_skill_md_as_bundled_file(tmp_path):
    skill = tmp_path / "skill"
    skill.mkdir()
    write_skill_md(skill / "SKILL.md", {"name": "skill", "description": "d"}, "body")
    with pytest.raises(ValueError, match="escapes skill root"):
        write_components(skill, {
            "description": "d", "body": "safe", "file:SKILL.md": "unstructured overwrite"
        })
    assert "body" in (skill / "SKILL.md").read_text()
    assert "safe" not in (skill / "SKILL.md").read_text()


def test_langfuse_proxy_has_fixed_identity_and_loopback_default():
    caddyfile = (ROOT / "ops/caddy/Langfuse.Caddyfile").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "on_demand" not in caddyfile
    assert "{$LANGFUSE_HOST:localhost}" in caddyfile
    assert "${LANGFUSE_BIND_ADDRESS:-127.0.0.1}:3443:3443" in compose


def test_external_langfuse_override_disables_bundled_stack():
    override = (ROOT / "docker-compose.external-langfuse.yml").read_text()

    assert "optimize-mine:\n    depends_on: !reset {}" in override
    assert override.count('profiles: !override ["bundled-langfuse"]') == 7
