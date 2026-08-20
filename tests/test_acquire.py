"""Offline Git fixtures for public GitHub acquisition.

The tests replace only the OWNER/REPO-to-URL mapping. Resolution, clone, tree inspection, and raw
blob reads all run through real Git against a local repository.
"""
import subprocess
from pathlib import Path

import pytest

from ingot import acquire


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def _remote(tmp_path: Path, *, asset: bytes = b"first bytes") -> tuple[Path, str]:
    remote = tmp_path / "remote"
    subprocess.run(["git", "init", "-b", "main", str(remote)], check=True,
                   capture_output=True)
    _git(remote, "config", "user.email", "fixture@example.test")
    _git(remote, "config", "user.name", "Fixture")
    skill = remote / "skills" / "pdf"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: Merge PDFs.\n---\n\nUse this skill.\n",
        encoding="utf-8")
    (skill / "asset.bin").write_bytes(asset)
    (remote / "outside.txt").write_text("not part of the package", encoding="utf-8")
    _git(remote, "add", ".")
    _git(remote, "commit", "-m", "Create fixture")
    return remote, _git(remote, "rev-parse", "HEAD")


def test_github_acquisition_resolves_commit_and_checks_out_only_the_selected_package(
        tmp_path, monkeypatch):
    """Break caught: recording an unverified ref, or admitting the repository root."""
    remote, commit = _remote(tmp_path)
    monkeypatch.setattr(acquire, "_remote_url", lambda repository: remote.as_uri())

    package, provenance = acquire.github(
        "acme/skills", ref="HEAD", subdirectory="skills/pdf", destination=tmp_path / "clone")

    assert package == tmp_path / "clone" / "package"
    assert (package / "asset.bin").read_bytes() == b"first bytes"
    assert not (package / "outside.txt").exists()
    assert provenance == {
        "repository": "acme/skills",
        "ref": "HEAD",
        "commit": commit,
        "subdirectory": "skills/pdf",
    }


@pytest.mark.parametrize("subdirectory", ["../pdf", "/skills/pdf", "skills\\pdf", "skills/\npdf"])
def test_github_acquisition_refuses_a_subdirectory_that_can_escape_or_change_spelling(
        tmp_path, subdirectory):
    """Break caught: joining an unchecked remote-controlled path onto the clone root."""
    with pytest.raises(ValueError, match="path|root|portable"):
        acquire.github("acme/skills", ref="HEAD", subdirectory=subdirectory,
                       destination=tmp_path / "clone")


@pytest.mark.parametrize("repository", ["acme", "acme/skills/extra", "../skills", "acme\\skills"])
def test_github_acquisition_refuses_a_non_owner_repository_name(tmp_path, repository):
    """Break caught: accepting a URL or option where the public OWNER/REPO locator is required."""
    with pytest.raises(ValueError, match="OWNER/REPO"):
        acquire.github(repository, ref="HEAD", subdirectory="skills/pdf",
                       destination=tmp_path / "clone")


def test_github_acquisition_refuses_a_repository_with_a_git_suffix(tmp_path, monkeypatch):
    """Break caught: accepting repo.git and then fetching the unintended repo.git.git URL."""
    monkeypatch.setattr(
        acquire, "_resolved_commit",
        lambda remote, ref: pytest.fail("repository validation must precede resolution"))

    with pytest.raises(ValueError, match="OWNER/REPO"):
        acquire.github("acme/skills.git", ref="HEAD", subdirectory="skills/pdf",
                       destination=tmp_path / "clone")


def test_github_acquisition_refuses_a_symlink_recorded_by_git(tmp_path, monkeypatch):
    """Break caught: core.symlinks=false flattening a Git symlink before tree.build can see it."""
    remote, _ = _remote(tmp_path)
    (remote / "skills" / "pdf" / "link.md").symlink_to("SKILL.md")
    _git(remote, "add", ".")
    _git(remote, "commit", "-m", "Add a symlink")
    monkeypatch.setattr(acquire, "_remote_url", lambda repository: remote.as_uri())

    with pytest.raises(ValueError, match="symlink"):
        acquire.github("acme/skills", ref="HEAD", subdirectory="skills/pdf",
                       destination=tmp_path / "clone")


def test_github_acquisition_refuses_a_submodule_recorded_by_git(tmp_path, monkeypatch):
    """Break caught: treating a gitlink as package bytes or recursively acquiring another repo."""
    child = tmp_path / "child"
    subprocess.run(["git", "init", "-b", "main", str(child)], check=True,
                   capture_output=True)
    _git(child, "config", "user.email", "fixture@example.test")
    _git(child, "config", "user.name", "Fixture")
    (child / "payload.txt").write_text("submodule bytes", encoding="utf-8")
    _git(child, "add", ".")
    _git(child, "commit", "-m", "Create child")
    remote, _ = _remote(tmp_path)
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-C", str(remote), "submodule", "add",
         child.as_uri(), "skills/pdf/vendor"], check=True, capture_output=True)
    _git(remote, "commit", "-am", "Add a submodule")
    monkeypatch.setattr(acquire, "_remote_url", lambda repository: remote.as_uri())

    with pytest.raises(ValueError, match="regular file"):
        acquire.github("acme/skills", ref="HEAD", subdirectory="skills/pdf",
                       destination=tmp_path / "clone")


def test_github_acquisition_refuses_an_oversized_tree_before_checkout(tmp_path, monkeypatch):
    """Break caught: downloading selected blobs before enforcing the admission byte budget."""
    remote, _ = _remote(tmp_path, asset=b"0123456789")
    monkeypatch.setattr(acquire, "_remote_url", lambda repository: remote.as_uri())
    monkeypatch.setattr(acquire, "MAX_TREE_BYTES", 8)

    with pytest.raises(ValueError, match="at most 8 bytes"):
        acquire.github("acme/skills", ref="HEAD", subdirectory="skills/pdf",
                       destination=tmp_path / "clone")

    assert not (tmp_path / "clone" / "package" / "asset.bin").exists()


def test_github_acquisition_refuses_too_many_files_before_checkout(tmp_path, monkeypatch):
    """Break caught: enforcing only byte size lets an empty-file tree exhaust the filesystem."""
    remote, _ = _remote(tmp_path)
    (remote / "skills" / "pdf" / "extra.txt").touch()
    _git(remote, "add", ".")
    _git(remote, "commit", "-m", "Add another file")
    monkeypatch.setattr(acquire, "_remote_url", lambda repository: remote.as_uri())
    monkeypatch.setattr(acquire, "MAX_FILES", 2)

    with pytest.raises(ValueError, match="at most 2 files"):
        acquire.github("acme/skills", ref="HEAD", subdirectory="skills/pdf",
                       destination=tmp_path / "clone")

    assert not (tmp_path / "clone" / "package").exists()


def test_github_acquisition_refuses_when_the_ref_moves_before_clone(tmp_path, monkeypatch):
    """Break caught: recording the ls-remote commit while admitting a later checkout."""
    remote, old_commit = _remote(tmp_path)
    (remote / "skills" / "pdf" / "asset.bin").write_bytes(b"new bytes")
    _git(remote, "add", ".")
    _git(remote, "commit", "-m", "Move HEAD")
    monkeypatch.setattr(acquire, "_remote_url", lambda repository: remote.as_uri())
    monkeypatch.setattr(acquire, "_resolved_commit", lambda remote, ref: old_commit)

    with pytest.raises(ValueError, match="ref moved"):
        acquire.github("acme/skills", ref="HEAD", subdirectory="skills/pdf",
                       destination=tmp_path / "clone")


def test_github_acquisition_does_not_run_checkout_filters(tmp_path, monkeypatch):
    """Break caught: checkout executing a configured smudge command selected by .gitattributes."""
    remote, _ = _remote(tmp_path)
    (remote / "skills" / "pdf" / ".gitattributes").write_text(
        "*.bin filter=fixture\n", encoding="utf-8")
    _git(remote, "add", ".")
    _git(remote, "commit", "-m", "Select a checkout filter")
    marker = tmp_path / "filter-ran"
    monkeypatch.setattr(acquire, "_remote_url", lambda repository: remote.as_uri())
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.fixture.smudge")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"touch {marker}; cat")

    package, _ = acquire.github(
        "acme/skills", ref="HEAD", subdirectory="skills/pdf", destination=tmp_path / "clone")

    assert (package / "asset.bin").read_bytes() == b"first bytes"
    assert not marker.exists()
