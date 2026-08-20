"""`ingot review` — the deterministic, offline, read-only report.

Named for the CLI, not for `ingot.optimize.review`, which is the model-graded advisory pass and a
different thing entirely (tests/test_review.py covers that one). Nothing here may reach a model, a
key, a service, or the network."""
import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from ingot import cli, review
from ingot.parse import parse_raw


def _skill(root, name, description="Merge and split PDF files.", body="Do the thing.",
           **frontmatter):
    """Built line by line rather than from a dedented block: an interpolated multi-line field
    defeats `textwrap.dedent`, which silently leaves the delimiters indented and turns every
    package into a frontmatter-missing one. That cost a green test that was asserting nothing."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    fields = {"name": name, "description": description, **frontmatter}
    lines = ["---"] + [f"{key}: {value}" for key, value in fields.items()] + ["---", "", body, ""]
    (directory / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return directory


def _codes(section) -> list[str]:
    return [finding["code"] for finding in section["findings"]]


def _all_codes(result) -> list[str]:
    return [finding["code"]
            for section in result["sections"].values()
            for finding in section["findings"]]


# --- the raw diagnostic parser -------------------------------------------------------------

def test_raw_parser_reports_absent_frontmatter_instead_of_normalizing_it():
    """`ingot.mcp_server.registry.parse_skill` turns this into empty metadata on purpose, so the server
    keeps serving. A diagnostic parser that did the same would have nothing to report."""
    raw = parse_raw("Just a body, no frontmatter.\n")

    assert raw.frontmatter is None
    assert [f.code for f in raw.findings] == ["frontmatter-missing"]


def test_raw_parser_reports_the_yaml_error_rather_than_swallowing_it():
    raw = parse_raw("---\nname: pdf\ndescription: [unclosed\n---\n\nbody\n")

    assert raw.frontmatter is None
    assert [f.code for f in raw.findings] == ["frontmatter-invalid"]
    assert raw.findings[0].message


def test_raw_parser_reports_frontmatter_that_is_not_a_mapping():
    raw = parse_raw("---\n- one\n- two\n---\n\nbody\n")

    assert raw.frontmatter is None
    assert [f.code for f in raw.findings] == ["frontmatter-not-a-mapping"]


def test_raw_parser_keeps_a_good_document_intact():
    raw = parse_raw("---\nname: pdf\ndescription: Merge PDFs.\n---\n\nThe body.\n")

    assert raw.findings == []
    assert raw.frontmatter == {"name": "pdf", "description": "Merge PDFs."}
    assert raw.body == "The body."


# --- structural validity -------------------------------------------------------------------

def test_a_known_good_skill_is_valid(tmp_path):
    directory = _skill(tmp_path, "pdf")

    result = review.review_package(directory)

    assert result["valid"] is True
    assert result["errors"] == 0


def test_a_missing_skill_md_is_an_error(tmp_path):
    directory = tmp_path / "pdf"
    directory.mkdir()

    result = review.review_package(directory)

    assert result["valid"] is False
    assert "skill-md-missing" in _all_codes(result)


def test_invalid_frontmatter_is_an_error(tmp_path):
    directory = tmp_path / "pdf"
    directory.mkdir()
    (directory / "SKILL.md").write_text("---\ndescription: [unclosed\n---\n\nbody\n",
                                        encoding="utf-8")

    result = review.review_package(directory)

    assert result["valid"] is False
    assert "frontmatter-invalid" in _all_codes(result)


def test_an_empty_description_is_an_error(tmp_path):
    """The router keys on description. A skill without one is never loaded, so shipping it is a
    silent no-op rather than a degraded skill."""
    directory = _skill(tmp_path, "pdf", description="")

    result = review.review_package(directory)

    assert result["valid"] is False
    assert "description-empty" in _all_codes(result)


def test_an_invalid_slug_is_an_error(tmp_path):
    directory = tmp_path / "Not_A_Slug"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: Not_A_Slug\ndescription: Something.\n---\n\nbody\n", encoding="utf-8")

    result = review.review_package(directory)

    assert result["valid"] is False
    assert "name-invalid" in _all_codes(result)


def test_a_name_that_disagrees_with_the_directory_warns_without_failing(tmp_path):
    directory = tmp_path / "pdf"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: docx\ndescription: Something.\n---\n\nbody\n", encoding="utf-8")

    result = review.review_package(directory)

    assert result["valid"] is True
    assert "name-directory-mismatch" in _all_codes(result)


def test_a_dangling_file_reference_warns_without_failing(tmp_path):
    """A broken link makes the skill worse, not unrepresentable, so it must not block admission."""
    directory = _skill(tmp_path, "pdf", body="See [the guide](./guide.md) for details.")

    result = review.review_package(directory)

    assert result["valid"] is True
    assert "file-reference-missing" in _all_codes(result)


def test_a_resolvable_file_reference_is_not_reported(tmp_path):
    directory = _skill(tmp_path, "pdf", body="See [the guide](./guide.md) for details.")
    (directory / "guide.md").write_text("guide\n", encoding="utf-8")

    result = review.review_package(directory)

    assert "file-reference-missing" not in _all_codes(result)


def test_path_traversal_in_a_reference_is_an_error(tmp_path):
    directory = _skill(tmp_path, "pdf", body="See [outside](../../etc/passwd).")

    result = review.review_package(directory)

    assert result["valid"] is False
    assert "path-traversal" in _all_codes(result)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privilege on Windows")
def test_a_symlink_escaping_the_package_is_an_error(tmp_path):
    directory = _skill(tmp_path, "pdf")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (directory / "link.txt").symlink_to(outside)

    result = review.review_package(directory)

    assert result["valid"] is False
    assert "symlink-unsupported" in _all_codes(result)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privilege on Windows")
def test_a_symlink_inside_the_package_is_an_error_too(tmp_path):
    """Containment is not the question any more. Admission stages exact bytes, and a link is
    neither preserved (the vault would commit a path leading out of the library) nor followed (the
    artifact would quietly become a different shape than the one submitted)."""
    directory = _skill(tmp_path, "pdf")
    (directory / "real.txt").write_text("data\n", encoding="utf-8")
    (directory / "link.txt").symlink_to(directory / "real.txt")

    result = review.review_package(directory)

    assert result["valid"] is False
    assert "symlink-unsupported" in _all_codes(result)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privilege on Windows")
def test_a_directory_symlink_does_not_pull_in_what_it_points_at(tmp_path):
    """`rglob` follows directory symlinks. A package could otherwise absorb a whole tree from
    outside itself and the review would never mention the link."""
    directory = _skill(tmp_path, "pdf")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret\n", encoding="utf-8")
    (directory / "docs").symlink_to(outside, target_is_directory=True)

    result = review.review_package(directory)

    assert "symlink-unsupported" in _all_codes(result)
    assert result["sections"]["structural"]["file_count"] == 1


def test_a_non_portable_path_warns(tmp_path):
    directory = _skill(tmp_path, "pdf")
    (directory / "why:not.txt").write_text("data\n", encoding="utf-8")

    result = review.review_package(directory)

    assert "path-not-portable" in _all_codes(result)


def test_case_insensitive_path_collision_warns(tmp_path):
    """Two files that differ only by case survive here and collapse into one on macOS or Windows,
    which changes the package's content hash depending on who checked it out."""
    directory = _skill(tmp_path, "pdf")
    (directory / "Guide.md").write_text("one\n", encoding="utf-8")
    try:
        (directory / "guide.md").write_text("two\n", encoding="utf-8")
    except OSError:
        pytest.skip("filesystem rejected the pair")
    if len(list(directory.glob("*uide.md"))) < 2:
        pytest.skip("case-insensitive filesystem collapsed the pair")

    result = review.review_package(directory)

    assert "path-case-collision" in _all_codes(result)


def test_unicode_normalization_collision_warns(tmp_path):
    """The same filename in NFC and NFD is one file on macOS and two on Linux."""
    directory = _skill(tmp_path, "pdf")
    composed = unicodedata.normalize("NFC", "café.md")
    decomposed = unicodedata.normalize("NFD", "café.md")
    (directory / composed).write_text("one\n", encoding="utf-8")
    try:
        (directory / decomposed).write_text("two\n", encoding="utf-8")
    except OSError:
        pytest.skip("filesystem rejected the pair")
    if len(list(directory.glob("*.md"))) < 3:
        pytest.skip("filesystem normalized the pair")

    result = review.review_package(directory)

    assert "path-unicode-collision" in _all_codes(result)


def test_path_collision_logic_without_a_filesystem():
    """The two collision cases above cannot be staged on a case-insensitive filesystem, which is
    every macOS development machine, so they skip exactly where most of this code is written. This
    covers the same logic directly -- `_path_findings` never touches the disk."""
    package = Path("/pkg")
    files = [package / "SKILL.md", package / "Guide.md", package / "guide.md"]

    codes = [finding.code for finding in review._path_findings(package, files)]

    assert "path-case-collision" in codes


def test_unicode_collision_logic_without_a_filesystem():
    package = Path("/pkg")
    files = [package / unicodedata.normalize("NFC", "café.md"),
             package / unicodedata.normalize("NFD", "café.md")]

    codes = [finding.code for finding in review._path_findings(package, files)]

    assert "path-unicode-collision" in codes


def test_distinct_paths_do_not_collide():
    package = Path("/pkg")
    files = [package / "SKILL.md", package / "guide.md", package / "notes.md"]

    assert review._path_findings(package, files) == []


def test_the_report_carries_a_stable_content_revision(tmp_path):
    directory = _skill(tmp_path, "pdf")

    first = review.review_package(directory)
    second = review.review_package(directory)

    assert first["revision"] == second["revision"]
    assert len(first["revision"]) == 64


# --- supply-chain metadata -----------------------------------------------------------------

def test_absent_source_and_license_metadata_are_reported(tmp_path):
    directory = _skill(tmp_path, "pdf")

    section = review.review_package(directory)["sections"]["supply_chain"]

    assert "source-metadata-missing" in _codes(section)
    assert "license-metadata-missing" in _codes(section)


def test_declared_source_and_license_are_not_reported_missing(tmp_path):
    directory = _skill(tmp_path, "pdf", license="Apache-2.0", source="https://example.test/repo")

    section = review.review_package(directory)["sections"]["supply_chain"]

    assert "license-metadata-missing" not in _codes(section)
    assert "source-metadata-missing" not in _codes(section)


def test_an_executable_asset_is_reported(tmp_path):
    directory = _skill(tmp_path, "pdf")
    script = directory / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o755)

    section = review.review_package(directory)["sections"]["supply_chain"]

    assert "executable-asset" in _codes(section)


def test_a_remote_reference_is_reported(tmp_path):
    directory = _skill(tmp_path, "pdf", body="Fetch https://example.test/tool.sh and run it.")

    section = review.review_package(directory)["sections"]["supply_chain"]

    assert "remote-reference" in _codes(section)


def test_an_unpinned_mutable_reference_is_reported(tmp_path):
    directory = _skill(tmp_path, "pdf",
                       body="Read https://github.com/acme/tool/blob/main/README.md first.")

    section = review.review_package(directory)["sections"]["supply_chain"]

    assert "reference-unpinned" in _codes(section)


def test_supply_chain_findings_never_fail_the_package(tmp_path):
    """These are advisory. Nothing here claims to be a security verdict, so nothing here may
    make an otherwise valid package inadmissible."""
    directory = _skill(tmp_path, "pdf", body="Fetch https://example.test/tool.sh and run it.")
    script = directory / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)

    result = review.review_package(directory)

    assert result["valid"] is True


# --- collision -----------------------------------------------------------------------------

def test_collision_is_unmeasured_without_a_library_root(tmp_path):
    directory = _skill(tmp_path, "pdf")

    section = review.review_package(directory)["sections"]["collision"]

    assert section["status"] == review.UNMEASURED


def test_a_library_name_collision_is_reported(tmp_path):
    library = tmp_path / "library"
    _skill(library, "pdf")
    candidate = _skill(tmp_path / "candidate", "pdf")

    section = review.review_package(candidate, library_root=library)["sections"]["collision"]

    assert "name-collision" in _codes(section)


def test_no_library_collision_when_the_name_is_free(tmp_path):
    library = tmp_path / "library"
    _skill(library, "docx")
    candidate = _skill(tmp_path / "candidate", "pdf")

    section = review.review_package(candidate, library_root=library)["sections"]["collision"]

    assert section["status"] == review.MEASURED
    assert _codes(section) == []


def test_an_abandoned_staging_directory_is_not_a_collision(tmp_path):
    """Promotion and rollback stage a skill beside the live one as `.<name>.<hex>.stage`, each
    carrying a complete SKILL.md. A bare glob would report a crashed run's leftovers as a colliding
    skill, so this reads the library the same way the server does."""
    library = tmp_path / "library"
    _skill(library, "docx")
    stage = library / ".pdf.abc123.stage"
    stage.mkdir(parents=True)
    (stage / "SKILL.md").write_text("---\nname: pdf\ndescription: Staged.\n---\n\nbody\n",
                                    encoding="utf-8")
    candidate = _skill(tmp_path / "candidate", "pdf")

    section = review.review_package(candidate, library_root=library)["sections"]["collision"]

    assert _codes(section) == []


def test_many_remote_references_produce_one_finding(tmp_path):
    """A wall of identical warnings is how a reviewer learns to stop reading them."""
    body = "\n".join(f"See https://example.test/page-{n}" for n in range(12))
    directory = _skill(tmp_path, "pdf", body=body)

    section = review.review_package(directory)["sections"]["supply_chain"]

    assert _codes(section).count("remote-reference") == 1
    assert len(section["remote_references"]) == 12


def test_semantic_collision_is_reported_unmeasured_not_guessed(tmp_path):
    """Description shadowing needs the embedding router, which needs a model. The deterministic
    command must say so and name the command that measures it, never approximate it."""
    library = tmp_path / "library"
    _skill(library, "docx")
    candidate = _skill(tmp_path / "candidate", "pdf")

    section = review.review_package(candidate, library_root=library)["sections"]["collision"]

    assert section["semantic"]["status"] == review.UNMEASURED
    assert "routing_health" in section["semantic"]["measure_with"]


# --- activation and behavioral evidence ------------------------------------------------------

def test_activation_is_unmeasured_without_a_routing_suite(tmp_path):
    directory = _skill(tmp_path, "pdf")

    section = review.review_package(directory, evidence_root=tmp_path)["sections"]["activation"]

    assert section["status"] == review.UNMEASURED
    assert section["routing_cases"] == 0


def test_activation_reports_an_existing_suite_without_scoring_it(tmp_path):
    """A suite existing is a fact this command can establish offline. Whether the router loads the
    skill at the right time is not, so it stays UNMEASURED with the command that would answer it."""
    directory = _skill(tmp_path, "pdf")
    tasks = tmp_path / "ingot" / "optimize" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "pdf.yaml").write_text(
        "routing:\n  - prompt: merge two pdfs\n  - prompt: split a pdf\n", encoding="utf-8")

    section = review.review_package(directory, evidence_root=tmp_path)["sections"]["activation"]

    assert section["routing_cases"] == 2
    assert section["status"] == review.UNMEASURED
    assert "score" not in section


def test_behavioral_evidence_is_unmeasured_when_absent(tmp_path):
    directory = _skill(tmp_path, "pdf")

    section = review.review_package(directory, evidence_root=tmp_path)["sections"]["behavioral"]

    assert section["status"] == review.UNMEASURED


def test_existing_compatibility_evidence_is_surfaced_not_recomputed(tmp_path):
    """`ingot.optimize.compat` already measures skill vs no-skill lift and writes it to runs/compat.
    Review reads that file. It must never run a model to answer this."""
    directory = _skill(tmp_path, "pdf")
    compat = tmp_path / "runs" / "compat"
    compat.mkdir(parents=True)
    (compat / "pdf.json").write_text(json.dumps({
        "skill": "pdf", "tasks": 12, "judge": "test-judge",
        "models": {"openrouter/some-model": {"skill": 0.8, "baseline": 0.5, "lift": 0.3}},
    }), encoding="utf-8")

    section = review.review_package(directory, evidence_root=tmp_path)["sections"]["behavioral"]

    assert section["status"] == review.MEASURED
    assert section["tasks"] == 12
    assert section["models"]["openrouter/some-model"]["lift"] == 0.3


def test_review_reports_no_composite_score(tmp_path):
    """A single number invites the reward-hacking this whole product exists to prevent."""
    directory = _skill(tmp_path, "pdf")

    result = review.review_package(directory)

    assert "score" not in result
    assert "grade" not in result


# --- the command ----------------------------------------------------------------------------

def test_command_exits_zero_for_a_valid_package(tmp_path, capsys):
    directory = _skill(tmp_path, "pdf")

    code = cli.main(["review", str(directory)])

    assert code == 0
    assert "pdf" in capsys.readouterr().out


def test_command_exits_nonzero_for_an_invalid_package(tmp_path, capsys):
    directory = _skill(tmp_path, "pdf", description="")

    code = cli.main(["review", str(directory)])

    assert code != 0


def test_command_exits_zero_when_only_warnings_are_present(tmp_path, capsys):
    directory = _skill(tmp_path, "pdf", body="See [the guide](./guide.md).")

    code = cli.main(["review", str(directory)])

    assert code == 0


def test_command_json_is_the_versioned_payload(tmp_path, capsys):
    directory = _skill(tmp_path, "pdf")

    code = cli.main(["review", str(directory), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == review.REVIEW_SCHEMA
    assert set(payload["sections"]) == {"structural", "supply_chain", "collision",
                                        "activation", "behavioral"}


def test_command_on_a_missing_path_fails_cleanly(tmp_path, capsys):
    code = cli.main(["review", str(tmp_path / "nowhere")])

    assert code != 0
    assert "nowhere" in capsys.readouterr().err


def test_reviewing_stays_read_only(tmp_path):
    """Read-only is a promise, not a description. A command that writes while reporting is a
    command that can change what it is reporting on."""
    directory = _skill(tmp_path, "pdf")
    before = {path: path.stat().st_mtime_ns for path in sorted(tmp_path.rglob("*"))}

    review.review_package(directory)

    after = {path: path.stat().st_mtime_ns for path in sorted(tmp_path.rglob("*"))}
    assert before == after


def test_review_runs_without_the_heavy_stack(tmp_path):
    """The point of the milestone, enforced: no model, no key, no services, no network."""
    directory = _skill(tmp_path, "pdf")
    heavy = ["fastapi", "onnxruntime", "langgraph", "langfuse", "litellm", "fastembed", "ingot.optimize"]
    program = ("import sys, ingot.cli; "
               f"ingot.cli.main(['review', {str(directory)!r}, '--json']); "
               f"print([m for m in {heavy!r} if m in sys.modules], file=sys.stderr)")

    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stderr.strip().endswith("[]")


# --------------------------------------------------------------------------- artifact fidelity

def test_a_binary_asset_is_reported_but_not_refused(tmp_path):
    """Admission preserves it byte-for-byte, so it is a note. It is still the fact a reviewer most
    needs: text can be read before approval and a compiled asset cannot."""
    package = _skill(tmp_path, "pdf")
    (package / "assets").mkdir()
    (package / "assets" / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\xff\xfe" * 16)

    result = review.review_package(package)

    assert result["valid"] is True
    codes = [f["code"] for f in result["sections"]["structural"]["findings"]]
    assert "binary-asset" in codes
    assert result["sections"]["structural"]["binary_assets"] == ["assets/diagram.png"]


def test_the_finding_names_every_asset_a_reviewer_cannot_read(tmp_path):
    package = _skill(tmp_path, "pdf")
    for name in ("a.png", "b.pdf", "c.bin"):
        (package / name).write_bytes(b"\xff\xfe\x00")

    result = review.review_package(package)

    assert result["sections"]["structural"]["binary_assets"] == ["a.png", "b.pdf", "c.bin"]
    message = [f["message"] for f in result["sections"]["structural"]["findings"]
               if f["code"] == "binary-asset"][0]
    for name in ("a.png", "b.pdf", "c.bin"):
        assert name in message


def test_editor_and_vcs_metadata_is_not_reported_as_an_asset(tmp_path):
    """A finding that fires on `.DS_Store` is one people learn to scroll past."""
    package = _skill(tmp_path, "pdf")
    (package / ".DS_Store").write_bytes(b"\x00\x00\x00\x01")
    (package / ".git").mkdir()
    (package / ".git" / "index").write_bytes(b"DIRC\xff")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\xff\x00")

    result = review.review_package(package)

    assert result["sections"]["structural"]["binary_assets"] == []
    assert result["valid"] is True


def test_the_extension_is_not_what_decides(tmp_path):
    """An extension is a claim about a file; the point is to check the file. A `.md` of raw bytes
    is unreadable, and a `.bin` of UTF-8 is not."""
    package = _skill(tmp_path, "pdf")
    (package / "readable.bin").write_text("plain text\n", encoding="utf-8")
    (package / "unreadable.md").write_bytes(b"\xff\xfe\x00\x01")

    assert review.review_package(package)["sections"]["structural"]["binary_assets"] \
        == ["unreadable.md"]


def test_a_package_of_only_text_reports_no_assets(tmp_path):
    package = _skill(tmp_path, "pdf")
    (package / "references").mkdir()
    (package / "references" / "notes.md").write_text("# Notes\n")
    (package / "run.sh").write_text("#!/bin/sh\necho hi\n")

    assert review.review_package(package)["sections"]["structural"]["binary_assets"] == []
