"""Shared test isolation. `configured_roots` always puts the local authoring root (SKILLS_DIR)
first — even ahead of explicit roots — so any test that loads skills would also see whatever
`scripts/fetch_skills.sh` has put in ./skills (first caught on a checkout with 72 fetched skills:
9 failures + a multi-minute embedding stall). Point SKILLS_DIR at an empty per-test directory and
clear SKILL_ROUTER_PATHS so the suite is hermetic; tests that care about the local root patch it
themselves on top of this."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_local_skills_root(tmp_path_factory, monkeypatch):
    monkeypatch.setattr("mcp_server.registry.SKILLS_DIR", tmp_path_factory.mktemp("local-skills"))
    monkeypatch.delenv("SKILL_ROUTER_PATHS", raising=False)
