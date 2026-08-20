"""The `ingot` console entry point.

The first product experience must not require the full stack, so these tests care about two things
the rest of the suite cannot see: that the CLI runs as an installed console script, and that
importing it does not drag in FastAPI, ONNX, LangGraph, Langfuse, or the optimizer. A CLI that only
works from a repo checkout with every server dependency present is not the lightweight install this
milestone exists to deliver."""
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ingot import cli


def _skill(root, name, description, body="Do the thing."):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        textwrap.dedent(f"""\
        ---
        name: {name}
        description: {description}
        ---

        {body}
        """),
        encoding="utf-8")
    return directory


def test_list_reports_a_skill_in_an_explicit_root(tmp_path):
    _skill(tmp_path, "pdf", "Merge and split PDF files.")

    result = cli.list_library(tmp_path)

    assert [s["name"] for s in result["skills"]] == ["pdf"]
    assert result["skills"][0]["description"] == "Merge and split PDF files."
    assert len(result["skills"][0]["revision"]) == 64


def test_list_reports_the_roots_it_actually_read(tmp_path):
    """`configured_roots` always prepends the local authoring root, even ahead of an explicit one,
    so a caller who passes `--root` can still be shown skills from somewhere else. Naming every root
    in the payload is what makes that visible instead of baffling."""
    _skill(tmp_path, "pdf", "Merge and split PDF files.")

    result = cli.list_library(tmp_path)

    assert str(tmp_path.resolve()) in result["roots"]


def test_list_of_an_empty_root_is_not_an_error(tmp_path):
    result = cli.list_library(tmp_path)

    assert result["skills"] == []
    assert result["schema_version"] == cli.LIST_SCHEMA


def test_list_skips_a_skill_with_no_description(tmp_path):
    """The router keys on description; a skill without one is unroutable and the loader drops it.
    The CLI must report the same library the server would serve, not a more generous one."""
    _skill(tmp_path, "pdf", "Merge and split PDF files.")
    empty = tmp_path / "blank"
    empty.mkdir()
    (empty / "SKILL.md").write_text("---\nname: blank\n---\n\nbody\n", encoding="utf-8")

    result = cli.list_library(tmp_path)

    assert [s["name"] for s in result["skills"]] == ["pdf"]


def test_main_list_json_emits_the_versioned_payload(tmp_path, capsys):
    _skill(tmp_path, "pdf", "Merge and split PDF files.")

    code = cli.main(["list", "--root", str(tmp_path), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == cli.LIST_SCHEMA
    assert [s["name"] for s in payload["skills"]] == ["pdf"]


def test_main_list_human_output_names_the_skill(tmp_path, capsys):
    _skill(tmp_path, "pdf", "Merge and split PDF files.")

    code = cli.main(["list", "--root", str(tmp_path)])

    assert code == 0
    assert "pdf" in capsys.readouterr().out


def test_main_with_no_command_fails_rather_than_doing_something(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])

    assert exit_info.value.code != 0


def _console_script() -> str | None:
    """The `ingot` script installed beside the interpreter running these tests.

    Not `shutil.which`: a virtualenv that has not been activated is not on PATH, so searching PATH
    silently skips the one test that proves the entry point exists. Fall back to PATH for a system
    install."""
    beside = Path(sys.executable).parent / "ingot"
    return str(beside) if beside.exists() else shutil.which("ingot")


@pytest.mark.skipif(_console_script() is None,
                    reason="console script not installed; run `pip install -e .`")
def test_installed_console_script_runs(tmp_path):
    """In situ: the real entry point, not an in-process call. `--help` is the one command that must
    work before anything else is configured."""
    result = subprocess.run([_console_script(), "--help"], capture_output=True, text=True)

    assert result.returncode == 0
    assert "list" in result.stdout


def test_installed_console_script_lists_a_real_library(tmp_path):
    """The acceptance criterion for this PR, run the way a user runs it: the installed script, a
    real directory, no services and no Docker."""
    script = _console_script()
    if script is None:
        pytest.skip("console script not installed; run `pip install -e .`")
    _skill(tmp_path, "pdf", "Merge and split PDF files.")

    result = subprocess.run([script, "list", "--root", str(tmp_path), "--json"],
                            capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert "pdf" in [skill["name"] for skill in json.loads(result.stdout)["skills"]]


def test_importing_the_cli_stays_light():
    """A subprocess, because the rest of the suite has already imported the heavy stack in-process.
    This is the whole point of the milestone: `ingot list` must not need the server's dependencies."""
    heavy = ["fastapi", "onnxruntime", "langgraph", "langfuse", "litellm", "fastembed", "ingot.optimize"]
    program = ("import sys, ingot.cli; "
               f"print([m for m in {heavy!r} if m in sys.modules])")

    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
