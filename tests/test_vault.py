"""`ingot vault init`: the one bootstrap command a local deployment needs.

`ingot status`, the other half of the managed-deployment surface, is in test_status.py."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ingot import cli, vault


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


# --------------------------------------------------------------------------- vault init

def test_init_creates_a_committed_vault_on_main(tmp_path):
    result = vault.init_vault(tmp_path / "vault")

    created = tmp_path / "vault"
    assert result["status"] == "created"
    assert result["branch"] == "main"
    assert (created / "registry.json").read_text() == "{}\n"
    assert (created / "scripts" / "validate.py").is_file()
    assert _git(created, "status", "--porcelain") == ""
    assert _git(created, "rev-parse", "HEAD") == result["head"]


def test_init_is_idempotent(tmp_path):
    first = vault.init_vault(tmp_path / "vault")
    second = vault.init_vault(tmp_path / "vault")

    assert second["status"] == "unchanged"
    assert second["head"] == first["head"]
    assert second["added"] == []


def test_init_completes_an_existing_repository_without_rewriting_it(tmp_path):
    """Adopting a Git repository someone already keeps skills in must add what is missing and
    touch nothing else."""
    existing = tmp_path / "vault"
    existing.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(existing)], check=True, capture_output=True)
    _git(existing, "config", "user.email", "owner@test.invalid")
    _git(existing, "config", "user.name", "Owner")
    (existing / "registry.json").write_text('{"pdf": {"disposition": "keep"}}\n')
    _git(existing, "add", ".")
    _git(existing, "commit", "-m", "Existing vault")

    result = vault.init_vault(existing)

    assert result["status"] == "updated"
    assert "scripts/validate.py" in result["added"]
    assert json.loads((existing / "registry.json").read_text()) == {"pdf": {"disposition": "keep"}}


def test_init_refuses_a_non_empty_directory_that_is_not_a_repository(tmp_path):
    """Adopting a directory of loose skills would make the first commit look like a publication
    nobody approved."""
    loose = tmp_path / "skills"
    (loose / "pdf").mkdir(parents=True)
    (loose / "pdf" / "SKILL.md").write_text("---\nname: pdf\ndescription: x\n---\nbody\n")

    with pytest.raises(ValueError, match="not empty and is not a Git repository"):
        vault.init_vault(loose)


def test_the_default_validator_refuses_a_tree_the_server_could_not_serve(tmp_path):
    created = tmp_path / "vault"
    vault.init_vault(created)
    (created / "pdf").mkdir()
    (created / "pdf" / "SKILL.md").write_text("---\nname: other\ndescription: Merge.\n---\nbody\n")

    result = subprocess.run([sys.executable, "scripts/validate.py"], cwd=created,
                            capture_output=True, text=True)

    assert result.returncode == 1
    assert "!= directory 'pdf'" in result.stderr


def test_the_default_validator_accepts_a_well_formed_tree(tmp_path):
    created = tmp_path / "vault"
    vault.init_vault(created)
    (created / "pdf").mkdir()
    (created / "pdf" / "SKILL.md").write_text("---\nname: pdf\ndescription: Merge.\n---\nbody\n")

    assert subprocess.run([sys.executable, "scripts/validate.py"], cwd=created,
                          capture_output=True).returncode == 0


# --------------------------------------------------------------------------- the command line

def test_vault_init_from_the_command_line(tmp_path, capsys):
    assert cli.main(["vault", "init", str(tmp_path / "vault"), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == vault.VAULT_SCHEMA
    assert Path(payload["path"]) == (tmp_path / "vault").resolve()


def test_vault_init_reports_a_refusal_without_a_traceback(tmp_path, capsys):
    (tmp_path / "loose.txt").write_text("not a vault")

    assert cli.main(["vault", "init", str(tmp_path)]) == 1
    assert "ingot vault init:" in capsys.readouterr().err
