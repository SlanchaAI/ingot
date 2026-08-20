"""Where mutable state lives.

The defect these protect against is not hypothetical: a `pip install ingot` kept its review queue,
its receipts, and its served library inside `site-packages`, so an upgrade discarded them, a
read-only install could not start, and two deployments sharing one installation shared one queue."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ingot import paths


def _clear(monkeypatch):
    for name in (paths.HOME, paths.LIBRARY, paths.RUNS, paths.TASKS, paths.VAULT,
                 *paths.LEGACY.values(), "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)


def test_state_defaults_outside_the_installed_package(monkeypatch):
    """The whole point. Anything under the package directory is discarded by the next upgrade."""
    _clear(monkeypatch)

    for path in (paths.home(), paths.library(), paths.runs(), paths.tasks(), paths.vault()):
        assert not path.is_relative_to(paths.PACKAGE_ROOT), path


def test_the_default_is_xdg(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert paths.home() == tmp_path / "ingot"
    assert paths.runs() == tmp_path / "ingot" / "runs"


def test_without_xdg_it_falls_under_the_user_home(monkeypatch):
    _clear(monkeypatch)

    assert paths.home() == Path.home() / ".local" / "state" / "ingot"


def test_one_home_moves_every_store(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv(paths.HOME, str(tmp_path))

    assert paths.library() == tmp_path / "library"
    assert paths.runs() == tmp_path / "runs"
    assert paths.tasks() == tmp_path / "tasks"


@pytest.mark.parametrize("setting,accessor,default", [
    (paths.LIBRARY, paths.library, "library"),
    (paths.RUNS, paths.runs, "runs"),
    (paths.TASKS, paths.tasks, "tasks"),
])
def test_each_store_can_be_placed_on_its_own(monkeypatch, tmp_path, setting, accessor, default):
    """A container mounts each one separately; it does not get to choose a single parent."""
    _clear(monkeypatch)
    monkeypatch.setenv(paths.HOME, str(tmp_path / "home"))
    monkeypatch.setenv(setting, str(tmp_path / "elsewhere"))

    assert accessor() == tmp_path / "elsewhere"
    assert paths.home() == tmp_path / "home"


def test_the_vault_defaults_to_the_library(monkeypatch, tmp_path):
    """In the local backend the served checkout is the vault. Defaulting them apart would invent a
    projection step that does not exist."""
    _clear(monkeypatch)
    monkeypatch.setenv(paths.LIBRARY, str(tmp_path / "library"))

    assert paths.vault() == tmp_path / "library"

    monkeypatch.setenv(paths.VAULT, str(tmp_path / "vault"))
    assert paths.vault() == tmp_path / "vault"


@pytest.mark.parametrize("setting,legacy", sorted(paths.LEGACY.items()))
def test_the_pre_existing_names_still_work(monkeypatch, tmp_path, setting, legacy):
    """An existing deployment must not break on upgrade, and `ingot status` says which name won."""
    _clear(monkeypatch)
    monkeypatch.setenv(legacy, str(tmp_path / "old"))

    assert paths._env(setting) == str(tmp_path / "old")
    sources = " ".join(entry["source"] for entry in paths.resolved())
    assert legacy in sources and "deprecated" in sources


def test_the_explicit_setting_outranks_the_legacy_one(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv(paths.LIBRARY, str(tmp_path / "new"))
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "old"))

    assert paths.library() == tmp_path / "new"


def test_resolved_reports_where_every_path_came_from(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv(paths.HOME, str(tmp_path))
    monkeypatch.setenv(paths.RUNS, str(tmp_path / "elsewhere"))

    report = {entry["name"]: entry for entry in paths.resolved()}

    assert report["runs"]["source"] == paths.RUNS
    assert report["library"]["source"] == paths.HOME
    assert report["runs"]["path"] == str(tmp_path / "elsewhere")


def test_an_unwritable_parent_is_reported_rather_than_discovered_on_first_write(monkeypatch,
                                                                               tmp_path):
    """A path that does not exist yet is fine; one whose parent cannot be written is not, and
    finding out at the first approval is the stall this reports instead."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    real_access = paths.os.access
    monkeypatch.setattr(paths.os, "access", lambda path, mode: False
                        if Path(path) == locked else real_access(path, mode))
    _clear(monkeypatch)
    monkeypatch.setenv(paths.HOME, str(locked / "ingot"))
    try:
        report = {entry["name"]: entry for entry in paths.resolved()}
    finally:
        locked.chmod(0o700)

    assert report["runs"]["exists"] is False
    assert report["runs"]["writable"] is False


@pytest.mark.skipif(os.getuid() == 0, reason="root writes a mode-500 directory regardless")
def test_a_writable_missing_path_is_not_reported_as_a_problem(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv(paths.HOME, str(tmp_path / "not-created-yet"))

    report = {entry["name"]: entry for entry in paths.resolved()}

    assert report["runs"]["exists"] is False
    assert report["runs"]["writable"] is True


def test_legacy_state_is_reported_never_migrated(monkeypatch, tmp_path):
    """Moving someone's review queue on their behalf is a change to controlled state made by a
    process nobody asked to make it."""
    monkeypatch.setattr(paths, "PACKAGE_ROOT", tmp_path)
    assert paths.legacy_state() == []

    (tmp_path / "runs" / "pending").mkdir(parents=True)
    (tmp_path / "runs" / "pending" / "pdf.json").write_text("{}")

    assert paths.legacy_state() == [str(tmp_path / "runs")]


def test_an_empty_package_directory_is_not_legacy_state(monkeypatch, tmp_path):
    """A checkout ships `skills/.gitkeep`. Reporting that as leftover state would train people to
    ignore the warning."""
    monkeypatch.setattr(paths, "PACKAGE_ROOT", tmp_path)
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / ".gitkeep").touch()

    assert paths.legacy_state() == []


def test_an_installed_ingot_keeps_no_state_inside_the_package(tmp_path):
    """In situ, because this is exactly the failure a unit test cannot see: the process must be
    started somewhere other than the checkout, or the checkout's own directories answer for it."""
    script = ("import json, os, sys\n"
              "os.environ['XDG_STATE_HOME'] = sys.argv[1]\n"
              "from ingot import paths\n"
              "print(json.dumps([entry['path'] for entry in paths.resolved()]))\n")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("INGOT_", "SKILLS_DIR", "VAULT_DIR", "XDG_"))}
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)

    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=tmp_path,
                            capture_output=True, text=True, env=environment)

    assert result.returncode == 0, result.stderr
    import json
    for path in json.loads(result.stdout):
        assert path.startswith(str(tmp_path)), path
        assert not Path(path).is_relative_to(paths.PACKAGE_ROOT), path
