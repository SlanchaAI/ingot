"""The candidate tree: the exact bytes an admitted package publishes.

Every test here exists because the alternative was silent. A package used to be reduced to decoded
text on the way in, so a file the dictionary could not hold was reviewed as part of the candidate
and then was not in it -- no error, no warning, just a revision naming a package that no longer
existed. These check that the bytes survive, that the receipt binds them, and that anything which
moves them afterwards is refused rather than published."""
import hashlib
import os
import stat

import pytest

from ingot.mcp_server.registry import skill_revision
from ingot.optimize import tree

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


@pytest.fixture(autouse=True)
def _runs(tmp_path, monkeypatch):
    monkeypatch.setenv("INGOT_RUNS", str(tmp_path / "runs"))


def _package(root, name="pdf", description="Merge and split PDF files.", body="Combine PDFs."):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8")
    return directory


def _components(description="Merge and split PDF files.", body="Combine PDFs."):
    import json
    return {"description": description, "body": body,
            "frontmatter": json.dumps({"name": "pdf", "description": description})}


# --- describing a package ---------------------------------------------------------------------

def test_every_regular_file_is_described_by_its_raw_bytes(tmp_path):
    package = _package(tmp_path / "src")
    (package / "assets").mkdir()
    (package / "assets" / "logo.png").write_bytes(PNG)

    manifest = tree.build(package)

    entry = next(item for item in manifest["files"] if item["path"] == "assets/logo.png")
    assert entry == {"path": "assets/logo.png", "mode": 0o644, "size": len(PNG),
                     "sha256": hashlib.sha256(PNG).hexdigest()}


def test_the_hash_is_of_bytes_not_of_decoded_text(tmp_path):
    """A file that is not valid UTF-8 has no decoded form, and one that is would hash differently
    after a round trip through `errors='ignore'` -- which is how the bytes went missing."""
    package = _package(tmp_path / "src")
    raw = b"caf\xe9 latin-1, not utf-8\n"
    (package / "notes.txt").write_bytes(raw)

    entry = next(item for item in tree.build(package)["files"] if item["path"] == "notes.txt")

    assert entry["sha256"] == hashlib.sha256(raw).hexdigest()


def test_modes_are_clamped_to_the_two_a_git_checkout_reproduces(tmp_path):
    package = _package(tmp_path / "src")
    (package / "run.sh").write_text("#!/bin/sh\n")
    (package / "run.sh").chmod(0o764)
    (package / "notes.md").write_text("# Notes\n")
    (package / "notes.md").chmod(0o600)

    modes = {item["path"]: item["mode"] for item in tree.build(package)["files"]}

    assert modes["run.sh"] == 0o755
    assert modes["notes.md"] == 0o644


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privilege on Windows")
def test_a_symlink_is_refused_by_name(tmp_path):
    package = _package(tmp_path / "src")
    (package / "real.md").write_text("# Real\n")
    (package / "link.md").symlink_to(package / "real.md")

    with pytest.raises(ValueError, match="symlinks are not admissible: link.md"):
        tree.build(package)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privilege on Windows")
def test_a_directory_symlink_is_refused_before_it_is_descended(tmp_path):
    package = _package(tmp_path / "src")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret\n")
    (package / "docs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinks are not admissible: docs"):
        tree.build(package)


def test_an_unportable_path_is_refused(tmp_path):
    package = _package(tmp_path / "src")
    (package / "a:b.md").write_text("# Colon\n")

    with pytest.raises(ValueError, match="portable POSIX path"):
        tree.build(package)


def test_the_byte_budget_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(tree, "MAX_TREE_BYTES", 128)
    package = _package(tmp_path / "src")
    (package / "big.bin").write_bytes(b"\x00" * 512)

    with pytest.raises(ValueError, match="at most 128 bytes"):
        tree.build(package)


def test_the_file_budget_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(tree, "MAX_FILES", 3)
    package = _package(tmp_path / "src")
    for index in range(5):
        (package / f"note-{index}.md").write_text("# Note\n")

    with pytest.raises(ValueError, match="at most 3 files"):
        tree.build(package)


# --- binding the manifest ---------------------------------------------------------------------

def test_the_digest_covers_the_file_list(tmp_path):
    """The digest is what binds a receipt to a staged tree. A receipt whose file list was edited
    without its digest would publish a tree nobody approved."""
    package = _package(tmp_path / "src")
    manifest = tree.build(package)
    manifest["files"][0]["sha256"] = "0" * 64

    with pytest.raises(ValueError, match="digest does not cover its file list"):
        tree.verify_manifest(manifest)


def test_a_manifest_entry_that_escapes_the_skill_root_is_refused(tmp_path):
    package = _package(tmp_path / "src")
    manifest = tree.build(package)
    manifest["files"][0]["path"] = "../escape.md"
    manifest["digest"] = tree._digest(manifest["files"])

    with pytest.raises(ValueError, match="escapes skill root"):
        tree.verify_manifest(manifest)


def test_a_mode_the_vault_cannot_serve_is_refused(tmp_path):
    package = _package(tmp_path / "src")
    manifest = tree.build(package)
    manifest["files"][0]["mode"] = 0o777
    manifest["digest"] = tree._digest(manifest["files"])

    with pytest.raises(ValueError, match="unsupported mode"):
        tree.verify_manifest(manifest)


# --- staging ----------------------------------------------------------------------------------

def test_staging_copies_the_exact_bytes(tmp_path):
    package = _package(tmp_path / "src")
    (package / "logo.png").write_bytes(PNG)
    manifest = tree.build(package)

    staged = tree.stage(package, manifest)

    assert (staged / "logo.png").read_bytes() == PNG
    assert staged == tree.staged_dir(manifest["digest"])


def test_staging_identical_bytes_twice_converges_on_one_directory(tmp_path):
    """Named by digest, so a resubmission of the same package is not a second copy or a race."""
    first = _package(tmp_path / "one")
    second = _package(tmp_path / "two")

    one = tree.stage(first, tree.build(first))
    two = tree.stage(second, tree.build(second))

    assert one == two
    assert len(list(tree.candidates_dir().iterdir())) == 1


def test_a_failed_staging_leaves_nothing_behind(tmp_path):
    package = _package(tmp_path / "src")
    manifest = tree.build(package)
    (package / "SKILL.md").write_text("changed after the manifest was built\n")

    with pytest.raises(ValueError, match="changed while it was being staged"):
        tree.stage(package, manifest)

    assert list(tree.candidates_dir().iterdir()) == []


# --- materializing ------------------------------------------------------------------------------

def test_materializing_reproduces_the_bytes_and_the_mode(tmp_path):
    package = _package(tmp_path / "src")
    (package / "logo.png").write_bytes(PNG)
    (package / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (package / "run.sh").chmod(0o755)
    manifest = tree.build(package)
    tree.stage(package, manifest)

    destination = tmp_path / "out"
    tree.materialize(manifest, destination)

    assert (destination / "logo.png").read_bytes() == PNG
    assert stat.S_IMODE((destination / "run.sh").stat().st_mode) == 0o755


def test_a_staged_file_altered_after_approval_is_refused(tmp_path):
    """The receipt is the authority. If the staged bytes have moved since it was written, the
    publisher must refuse rather than publish whatever it finds."""
    package = _package(tmp_path / "src")
    (package / "logo.png").write_bytes(PNG)
    manifest = tree.build(package)
    staged = tree.stage(package, manifest)
    (staged / "logo.png").write_bytes(b"something else")

    with pytest.raises(ValueError, match="does not match the receipt: logo.png"):
        tree.materialize(manifest, tmp_path / "out")


def test_a_missing_staged_tree_is_refused_not_skipped(tmp_path):
    package = _package(tmp_path / "src")
    manifest = tree.build(package)

    with pytest.raises(ValueError, match="staged candidate tree .* is missing"):
        tree.materialize(manifest, tmp_path / "out")


# --- what the library ends up serving -----------------------------------------------------------

def test_skill_md_is_normalized_and_everything_else_is_preserved(tmp_path):
    """The one documented exception. SKILL.md's frontmatter is the routing interface, so it is
    re-emitted through a safe YAML dump; every other file is the operator's bytes."""
    package = _package(tmp_path / "src")
    (package / "logo.png").write_bytes(PNG)
    manifest = tree.build(package)
    tree.stage(package, manifest)

    destination = tmp_path / "out" / "pdf"
    tree.materialize_creation(manifest, _components(description="Merge   and   split."),
                              "pdf", destination)

    assert (destination / "logo.png").read_bytes() == PNG
    assert "description: Merge and split." in (destination / "SKILL.md").read_text()


def test_the_approved_revision_is_the_revision_of_the_materialized_tree(tmp_path):
    """Computed by materializing it. A revision derived some other way would be a second
    description of the same bytes, and the two would eventually disagree."""
    package = _package(tmp_path / "src")
    (package / "logo.png").write_bytes(PNG)
    manifest = tree.build(package)
    tree.stage(package, manifest)
    components = _components()

    revision = tree.revision("pdf", manifest, components)

    destination = tmp_path / "out" / "pdf"
    tree.materialize_creation(manifest, components, "pdf", destination)
    assert revision == skill_revision(destination)


def test_one_changed_asset_byte_changes_the_revision(tmp_path):
    """While assets were dropped, two packages differing only in an image hashed identically."""
    first = _package(tmp_path / "one")
    (first / "logo.png").write_bytes(PNG)
    second = _package(tmp_path / "two")
    (second / "logo.png").write_bytes(PNG[:-1] + b"\x00")

    revisions = set()
    for package in (first, second):
        manifest = tree.build(package)
        tree.stage(package, manifest)
        revisions.add(tree.revision("pdf", manifest, _components()))

    assert len(revisions) == 2
