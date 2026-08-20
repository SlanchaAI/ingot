"""What the repository ships.

A release controller must not arrive with an unexplained pending change already in its queue, and
must not ship a served skill nobody approved. These read what git tracks rather than what happens
to be in one working tree, because that is what a clone gets."""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _tracked(pathspec: str) -> list[str]:
    result = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--", pathspec],
                            capture_output=True, text=True)
    if result.returncode:
        pytest.skip("not a git checkout")
    return [line for line in result.stdout.splitlines() if line]


def test_a_clean_checkout_begins_with_no_live_pending_proposal():
    """A tracked pending record would arrive as a real quarantined change in every clone: it would
    show in `ingot pending`, be approvable, and publish something nobody proposed."""
    assert _tracked("runs") == []


def test_a_clean_checkout_serves_no_skill_it_did_not_publish():
    """The demo library ships empty. A tracked skill would be served with no release receipt behind
    it, which is precisely the UNMANAGED state `ingot status` exists to report."""
    assert _tracked("skills") == ["skills/.gitkeep"]


def test_the_vault_is_never_tracked():
    """`ingot vault init` makes it a Git repository of its own; a tracked copy would be a second,
    stale answer to what is published."""
    assert _tracked("vault") == []


@pytest.mark.parametrize("pathspec", ["runs", "skills/*/", "vault"])
def test_state_paths_are_ignored_so_a_live_deployment_cannot_commit_itself(pathspec):
    """A checkout used as a deployment writes into these. Without ignore rules the first `git add`
    would commit a review queue and a set of receipts into the product."""
    probe = {"runs": "runs/pending/probe.json",
             "skills/*/": "skills/probe/SKILL.md",
             "vault": "vault/registry.json"}[pathspec]
    result = subprocess.run(["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT),
                             "check-ignore", "-q", probe],
                            capture_output=True)

    assert result.returncode == 0, f"{probe} is not ignored"


def test_the_package_claims_one_installed_name():
    """`mcp_server` and `optimize` are far too generic to own on PyPI. They now live under `ingot`,
    and a shim would have been self-defeating: a shim named `optimize` still claims `optimize`."""
    import tomllib

    configured = tomllib.loads((ROOT / "pyproject.toml").read_text())
    packages = configured["tool"]["setuptools"]["packages"]

    assert [name for name in packages if not name.startswith("ingot")] == []


@pytest.mark.parametrize("name", ["mcp_server", "optimize"])
def test_no_generic_package_reappears_at_the_repository_root(name):
    """A stale `build/` directory from before the move will happily reinstall the old top-level
    packages, so this checks what git tracks rather than what happens to be on disk."""
    assert _tracked(name) == []
