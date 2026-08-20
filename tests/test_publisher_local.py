"""The local publication backend: a Git vault on this machine, no network at any point.

Every test here runs offline by construction — the vault has no remote to reach — and several of
them assert that explicitly, because "air-gappable" is a claim the code has to keep rather than a
property of how the test happened to be written.
"""
import json
import subprocess
from pathlib import Path

import pytest

from ingot.mcp_server.registry import load_skills, optimizable_components, skill_revision
from ingot.optimize import promote as P
from ingot.optimize import publication as Q
from ingot.optimize import publisher as W
from ingot import delivery as D


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _vault(tmp_path, monkeypatch):
    """A vault with no remote at all. Nothing here has ever heard of GitHub."""
    vault = tmp_path / "vault"
    subprocess.run(["git", "init", "-b", "main", str(vault)], check=True, capture_output=True)
    _git(vault, "config", "user.name", "Vault Owner")
    _git(vault, "config", "user.email", "owner@test.invalid")
    skill = vault / "pdf"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: pdf\ndescription: Merge PDFs.\n---\nold body\n")
    (vault / "registry.json").write_text(
        json.dumps({"pdf": {"disposition": "keep", "reason": "test"}}) + "\n")
    (vault / "scripts").mkdir()
    (vault / "scripts" / "validate.py").write_text("print('valid')\n")
    _git(vault, "add", ".")
    _git(vault, "commit", "-m", "Initial vault")
    monkeypatch.setenv("INGOT_LIBRARY", str(vault))
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(vault))
    return vault, skill


def _queue(skill, body="new body"):
    champion = optimizable_components(skill)
    challenger = {**champion, "body": body}
    current = load_skills(skill.parent)[0]
    pending = {
        "skill": "pdf", "champion_components": champion,
        "challenger_components": challenger, "gate": {"promotable": True, "blocked": []},
        "evidence": {"champion": {"revision": current.revision},
                     "challenger": {"revision": skill_revision(skill, challenger)}},
    }
    P.save_pending("pdf", pending)
    return Q.queue_publication("pdf", pending, "admin", "promote")


def _publisher(vault):
    return W.Publisher(vault, backend=W.LocalBackend())


def _forbid_network(monkeypatch):
    """Fail loudly on anything that would leave the machine."""
    run = W._run

    def offline(command, *, cwd):
        if command[0] == "gh" or command[:2] == ["git", "push"] or "ls-remote" in command:
            raise AssertionError(f"the local backend reached the network: {' '.join(command)}")
        return run(command, cwd=cwd)

    monkeypatch.setattr(W, "_run", offline)


# --------------------------------------------------------------------------- the happy path

def test_a_vault_with_no_remote_publishes_and_activates_in_one_pass(tmp_path, monkeypatch):
    """There is no `awaiting_merge`: nothing external has to agree before the bytes are served."""
    vault, skill = _vault(tmp_path, monkeypatch)
    _forbid_network(monkeypatch)
    receipt = _queue(skill)
    record = Q.load_publication(receipt.id)

    assert _publisher(vault).process(receipt.id) == "active"

    assert "new body" in (skill / "SKILL.md").read_text()
    assert skill_revision(skill) == record["candidate_revision"]
    assert not P.pending_path("pdf").exists()
    assert (P.revisions_dir() / "pdf" / record["expected_champion"]).is_dir()
    stored = Q.load_publication(receipt.id)
    assert stored["state"] == "active"
    assert stored["merged_commit"] == _git(vault, "rev-parse", "HEAD")


def test_approval_alone_leaves_the_served_checkout_byte_identical(tmp_path, monkeypatch):
    vault, skill = _vault(tmp_path, monkeypatch)
    before = (skill / "SKILL.md").read_bytes()
    head = _git(vault, "rev-parse", "HEAD")

    _queue(skill)

    assert (skill / "SKILL.md").read_bytes() == before
    assert _git(vault, "rev-parse", "HEAD") == head


def test_the_vault_commit_is_authored_by_the_publisher_not_the_host(tmp_path, monkeypatch):
    """The commit records who published, not whoever happens to own the shell."""
    vault, skill = _vault(tmp_path, monkeypatch)
    receipt = _queue(skill)

    assert _publisher(vault).process(receipt.id) == "active"

    assert _git(vault, "log", "-1", "--format=%an <%ae>") == "Ingot Publisher <ingot@local.invalid>"
    assert _git(vault, "log", "-1", "--format=%s") == f"Publish pdf via Ingot {receipt.id}"


def test_activation_retires_the_publication_branch(tmp_path, monkeypatch):
    vault, skill = _vault(tmp_path, monkeypatch)
    receipt = _queue(skill)

    assert _publisher(vault).process(receipt.id) == "active"

    assert _git(vault, "branch", "--list", f"ingot/{receipt.id}") == ""


def test_republishing_a_revision_the_vault_already_serves_is_a_no_op(tmp_path, monkeypatch):
    """An empty diff must converge on the existing tip. `git commit` with nothing staged exits
    non-zero, so treating this as an error would fail a receipt that has nothing left to do."""
    vault, skill = _vault(tmp_path, monkeypatch)
    receipt = _queue(skill)
    publisher = _publisher(vault)
    assert publisher.process(receipt.id) == "active"
    head = _git(vault, "rev-parse", "HEAD")

    Q.update_publication(receipt.id, state="approved_publishing")
    assert publisher.process(receipt.id) == "active"

    assert _git(vault, "rev-parse", "HEAD") == head


def test_an_absence_rollback_removes_the_skill_and_its_registry_entry(tmp_path, monkeypatch):
    vault, skill = _vault(tmp_path, monkeypatch)
    P._snapshot_absence("pdf")
    P.rollback("pdf", P.ABSENT_REVISION, actor="admin")
    receipt = Q.publication_for_skill("pdf")

    assert _publisher(vault).process(receipt["id"]) == "active"

    assert not skill.exists()
    assert "pdf" not in json.loads((vault / "registry.json").read_text())


def test_a_rollback_restores_the_stored_snapshot(tmp_path, monkeypatch):
    vault, skill = _vault(tmp_path, monkeypatch)
    champion = load_skills(vault)[0].revision
    assert _publisher(vault).process(_queue(skill).id) == "active"
    assert "new body" in (skill / "SKILL.md").read_text()

    P.rollback("pdf", champion, actor="admin")
    receipt = Q.publication_for_skill("pdf")

    assert _publisher(vault).process(receipt["id"]) == "active"

    assert "old body" in (skill / "SKILL.md").read_text()
    assert skill_revision(skill) == champion


# --------------------------------------------------------------------------- recovery

def test_a_leftover_worktree_is_destroyed_rather_than_reused(tmp_path, monkeypatch):
    vault, skill = _vault(tmp_path, monkeypatch)
    receipt = _queue(skill)
    workspace = Q.publications_dir() / "worktrees" / receipt.id
    workspace.mkdir(parents=True)
    (workspace / "STRAY.md").write_text("left behind by a killed run\n")

    assert _publisher(vault).process(receipt.id) == "active"

    assert not (vault / "STRAY.md").exists()
    assert "new body" in (skill / "SKILL.md").read_text()


class _AdvanceFails(W.LocalBackend):
    """A crash after the branch is committed and before the served checkout moves."""

    def advance(self, repo, record, commit):
        raise RuntimeError("killed between the commit and the fast-forward")


class _AdvanceThenCrash(W.LocalBackend):
    """A crash after the served checkout moves and before the receipt is written."""

    def advance(self, repo, record, commit):
        super().advance(repo, record, commit)
        raise RuntimeError("killed after the fast-forward")


def test_a_crash_before_the_fast_forward_resumes_from_the_committed_branch(tmp_path, monkeypatch):
    vault, skill = _vault(tmp_path, monkeypatch)
    receipt = _queue(skill)
    with pytest.raises(RuntimeError, match="killed between"):
        W.Publisher(vault, backend=_AdvanceFails()).process(receipt.id)

    record = Q.load_publication(receipt.id)
    assert record["state"] == "publishing"
    assert "old body" in (skill / "SKILL.md").read_text()
    assert _git(vault, "rev-parse", f"refs/heads/ingot/{receipt.id}") == record["branch_commit"]

    assert _publisher(vault).process(receipt.id) == "active"
    assert "new body" in (skill / "SKILL.md").read_text()
    assert not P.pending_path("pdf").exists()


def test_a_crash_after_the_fast_forward_finalizes_instead_of_resnapshotting(tmp_path, monkeypatch):
    """The served bytes already equal the candidate, so the champion this would snapshot is gone.
    Re-snapshotting would refuse a publication that has in fact already activated."""
    vault, skill = _vault(tmp_path, monkeypatch)
    receipt = _queue(skill)
    with pytest.raises(RuntimeError, match="killed after"):
        W.Publisher(vault, backend=_AdvanceThenCrash()).process(receipt.id)

    assert "new body" in (skill / "SKILL.md").read_text()      # the fast-forward survived
    assert Q.load_publication(receipt.id)["state"] == "publishing"
    assert P.pending_path("pdf").exists()                      # but nothing was finalized

    assert _publisher(vault).process(receipt.id) == "active"
    assert not P.pending_path("pdf").exists()


def test_an_active_receipt_is_never_reprocessed(tmp_path, monkeypatch):
    vault, skill = _vault(tmp_path, monkeypatch)
    receipt = _queue(skill)
    publisher = _publisher(vault)
    assert publisher.process(receipt.id) == "active"
    head = _git(vault, "rev-parse", "HEAD")

    assert publisher.process(receipt.id) == "active"
    assert _git(vault, "rev-parse", "HEAD") == head


# --------------------------------------------------------------------------- divergence

def test_advance_refuses_a_branch_that_no_longer_fast_forwards(tmp_path, monkeypatch):
    """Never a rebase, a merge commit, or a reset. The vault has other legitimate writers, and a
    publisher that forces past one of them is no longer the only writer of what it serves."""
    vault, skill = _vault(tmp_path, monkeypatch)
    receipt = _queue(skill)
    with pytest.raises(RuntimeError, match="killed between"):
        W.Publisher(vault, backend=_AdvanceFails()).process(receipt.id)
    branch = Q.load_publication(receipt.id)["branch"]
    (vault / "NOTES.md").write_text("someone committed to the vault directly\n")
    _git(vault, "add", "NOTES.md")
    _git(vault, "commit", "-m", "A direct vault commit")
    head = _git(vault, "rev-parse", "HEAD")

    with pytest.raises(RuntimeError, match="git merge"):
        W.LocalBackend().advance(W.VaultRepo(vault), {"id": receipt.id}, branch)

    assert _git(vault, "rev-parse", "HEAD") == head            # nothing moved
    assert "old body" in (skill / "SKILL.md").read_text()


def test_a_publication_branch_left_behind_by_a_direct_vault_commit_is_recut(tmp_path, monkeypatch):
    """The receipt retries rather than wedging forever: the stale branch is abandoned, the
    publication is re-materialized against the vault as it now stands, and the unrelated commit
    survives."""
    vault, skill = _vault(tmp_path, monkeypatch)
    receipt = _queue(skill)
    with pytest.raises(RuntimeError, match="killed between"):
        W.Publisher(vault, backend=_AdvanceFails()).process(receipt.id)
    (vault / "NOTES.md").write_text("someone committed to the vault directly\n")
    _git(vault, "add", "NOTES.md")
    _git(vault, "commit", "-m", "A direct vault commit")

    assert _publisher(vault).process(receipt.id) == "active"

    assert "new body" in (skill / "SKILL.md").read_text()
    assert (vault / "NOTES.md").exists()


def test_a_champion_changed_out_of_band_is_refused_not_overwritten(tmp_path, monkeypatch):
    vault, skill = _vault(tmp_path, monkeypatch)
    receipt = _queue(skill)
    (skill / "SKILL.md").write_text("---\nname: pdf\ndescription: Merge PDFs.\n---\nedited\n")
    _git(vault, "add", "pdf")
    _git(vault, "commit", "-m", "An out-of-band edit to the champion")

    with pytest.raises(RuntimeError, match="champion does not match"):
        _publisher(vault).process(receipt.id)

    assert "edited" in (skill / "SKILL.md").read_text()
    assert P.pending_path("pdf").exists()


# --------------------------------------------------------------------------- configuration

def test_the_backend_defaults_to_local_and_is_never_inferred_from_a_remote(tmp_path):
    """A vault that gains an `origin` must not silently start opening pull requests."""
    config = W.load_config({"INGOT_VAULT_PATH": str(tmp_path)})
    assert config.backend == "local"
    assert isinstance(config.build().backend, W.LocalBackend)


def test_a_missing_vault_path_is_an_error_not_the_demo_directory(tmp_path):
    with pytest.raises(W.ConfigurationError, match="no vault configured"):
        W.load_config({})


def test_an_unknown_backend_is_refused_by_name(tmp_path):
    with pytest.raises(W.ConfigurationError, match="unknown publication backend"):
        W.load_config({"INGOT_VAULT_PATH": str(tmp_path), "INGOT_PUBLISH_BACKEND": "gitlab"})


def test_the_forge_backend_requires_a_repository(tmp_path):
    with pytest.raises(W.ConfigurationError, match="INGOT_FORGE_REPOSITORY"):
        W.load_config({"INGOT_VAULT_PATH": str(tmp_path), "INGOT_PUBLISH_BACKEND": "forge"})


def test_forge_settings_under_the_local_backend_warn_rather_than_look_active(tmp_path):
    config = W.load_config({"INGOT_VAULT_PATH": str(tmp_path),
                            "INGOT_FORGE_REPOSITORY": "someone/skills"})
    assert config.backend == "local"
    assert any("inert" in warning for warning in config.warnings)


def test_an_explicit_argument_outranks_the_environment(tmp_path):
    config = W.load_config({"INGOT_VAULT_PATH": str(tmp_path / "env"),
                            "INGOT_PUBLISH_BACKEND": "forge",
                            "INGOT_FORGE_REPOSITORY": "someone/skills"},
                           backend="local", vault=tmp_path / "explicit")
    assert config.backend == "local"
    assert config.vault_dir == tmp_path / "explicit"


def test_a_vault_without_a_validator_refuses_to_start(tmp_path, monkeypatch):
    """Every publication runs it before committing, so a missing one is a configuration error and
    not a silent skip."""
    vault, _ = _vault(tmp_path, monkeypatch)
    (vault / "scripts" / "validate.py").unlink()
    _git(vault, "commit", "-am", "Remove the validator")

    with pytest.raises(W.ConfigurationError, match="no validator"):
        W.validate(W.load_config({"INGOT_VAULT_PATH": str(vault)}))


@pytest.mark.parametrize("fault", ["dirty", "detached", "missing"])
def test_startup_refuses_a_vault_the_publisher_must_not_build_on(tmp_path, monkeypatch, fault):
    vault, _ = _vault(tmp_path, monkeypatch)
    if fault == "dirty":
        (vault / "dirty.txt").write_text("dirty")
    elif fault == "detached":
        _git(vault, "checkout", "--detach")
    else:
        vault = tmp_path / "nowhere"

    with pytest.raises(W.ConfigurationError):
        W.validate(W.load_config({"INGOT_VAULT_PATH": str(vault)}))


def test_a_valid_local_vault_starts(tmp_path, monkeypatch):
    vault, _ = _vault(tmp_path, monkeypatch)
    publisher = W.validate(W.load_config({"INGOT_VAULT_PATH": str(vault)}))
    assert publisher.vault_dir == Path(vault).resolve()
    assert publisher.backend.name == "local"


# ------------------------------------------------------------------ artifact fidelity, end to end

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + b"\xff\xfe\xfd"


def _ingested(tmp_path, monkeypatch, vault):
    """A real `ingot add` of a package carrying bytes no text component could hold."""
    from ingot import admission

    monkeypatch.setenv("INGOT_RUNS", str(tmp_path / "runs"))
    package = tmp_path / "src" / "csv-tidy"
    (package / "assets").mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: csv-tidy\ndescription: Tidy CSV files.\n---\n\nUse this to tidy CSVs.\n",
        encoding="utf-8")
    (package / "assets" / "logo.png").write_bytes(PNG)
    (package / "run.sh").write_text("#!/bin/sh\necho tidy\n", encoding="utf-8")
    (package / "run.sh").chmod(0o755)
    admission.add_package(package, actor="operator")
    return package


def test_an_ingested_binary_asset_reaches_the_vault_byte_for_byte(tmp_path, monkeypatch):
    """The end of the chain the whole change exists for. Admission used to reduce a package to
    decoded text, so this file was reviewed as part of the candidate, approved as part of the
    candidate, and then was not in the vault -- with no error anywhere along the way."""
    vault, _ = _vault(tmp_path, monkeypatch)
    _forbid_network(monkeypatch)
    _ingested(tmp_path, monkeypatch, vault)
    receipt = Q.queue_publication("csv-tidy", P.load_pending("csv-tidy"), "admin", "promote")

    assert _publisher(vault).process(receipt.id) == "active"

    published = vault / "csv-tidy" / "assets" / "logo.png"
    assert published.read_bytes() == PNG
    assert _git(vault, "status", "--porcelain") == ""
    assert skill_revision(vault / "csv-tidy") == Q.load_publication(receipt.id)["candidate_revision"]


def test_the_published_executable_bit_survives_the_vault_commit(tmp_path, monkeypatch):
    vault, _ = _vault(tmp_path, monkeypatch)
    _forbid_network(monkeypatch)
    _ingested(tmp_path, monkeypatch, vault)
    receipt = Q.queue_publication("csv-tidy", P.load_pending("csv-tidy"), "admin", "promote")

    _publisher(vault).process(receipt.id)

    entry = _git(vault, "ls-files", "-s", "csv-tidy/run.sh")
    assert entry.split()[0] == "100755"


def test_a_staged_asset_altered_after_approval_stops_the_publication(tmp_path, monkeypatch):
    """The receipt is the authority for what gets served. If the staged bytes moved between the
    approval and the publication, the publisher must refuse rather than publish what it finds."""
    from ingot.optimize import tree

    vault, _ = _vault(tmp_path, monkeypatch)
    _forbid_network(monkeypatch)
    _ingested(tmp_path, monkeypatch, vault)
    pending = P.load_pending("csv-tidy")
    receipt = Q.queue_publication("csv-tidy", pending, "admin", "promote")
    staged = tree.staged_dir(pending["tree"]["digest"])
    (staged / "assets" / "logo.png").write_bytes(b"substituted")

    with pytest.raises(RuntimeError, match="does not match the receipt: assets/logo.png"):
        _publisher(vault).process(receipt.id)

    assert not (vault / "csv-tidy").exists()
    assert _git(vault, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert "does not match the receipt" in Q.load_publication(receipt.id)["last_error"]


# --------------------------------------------------------------------------- delivery targets

def _delivering(vault, tmp_path, name="claude"):
    """A publisher that serves the managed vault and one native filesystem root beside it."""
    native = tmp_path / name
    targets = D.parse_targets(f"{name}=filesystem:{native}", vault=vault)
    return W.Publisher(vault, backend=W.LocalBackend(), targets=targets), native


def test_an_approved_revision_reaches_every_target(tmp_path, monkeypatch):
    """The whole point: the vault and a native skill root end up holding the same approved bytes,
    from one approval, through one publisher."""
    vault, skill = _vault(tmp_path, monkeypatch)
    _forbid_network(monkeypatch)
    publisher, native = _delivering(vault, tmp_path)
    receipt = _queue(skill)

    assert publisher.process(receipt.id) == "active"

    assert skill_revision(native / "pdf") == skill_revision(skill)
    assert (native / "pdf" / "SKILL.md").read_bytes() == (skill / "SKILL.md").read_bytes()


def test_each_target_is_recorded_on_the_receipt_separately(tmp_path, monkeypatch):
    vault, skill = _vault(tmp_path, monkeypatch)
    _forbid_network(monkeypatch)
    publisher, native = _delivering(vault, tmp_path)
    publisher.process(_queue(skill).id)

    delivered = Q.publication_for_skill("pdf")["delivery"]
    assert delivered["vault"]["kind"] == D.MANAGED_MCP
    assert delivered["claude"] == {"kind": D.FILESYSTEM, "root": str(native),
                                   "state": "delivered", "revision": skill_revision(skill),
                                   "at": delivered["claude"]["at"]}


def test_only_the_altered_target_reports_drift(tmp_path, monkeypatch):
    """Independent status. Editing one target must not make the other one look wrong."""
    vault, skill = _vault(tmp_path, monkeypatch)
    _forbid_network(monkeypatch)
    publisher, native = _delivering(vault, tmp_path)
    publisher.process(_queue(skill).id)
    released = skill_revision(skill)

    (native / "pdf" / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: Merge PDFs.\n---\nedited\n")

    observed = {target.name: D.observed(target, "pdf") for target in publisher.targets}
    assert observed["vault"] == released
    assert observed["claude"] != released


def test_a_rollback_returns_every_target_to_the_prior_revision(tmp_path, monkeypatch):
    """Rollback travels the ordinary publication queue, so delivery happens on the way through
    rather than needing a second mechanism that could disagree with the first."""
    vault, skill = _vault(tmp_path, monkeypatch)
    _forbid_network(monkeypatch)
    publisher, native = _delivering(vault, tmp_path)
    champion = load_skills(vault)[0].revision
    publisher.process(_queue(skill).id)
    assert skill_revision(native / "pdf") != champion

    P.rollback("pdf", champion, actor="admin")
    assert publisher.process(Q.publication_for_skill("pdf")["id"]) == "active"

    assert skill_revision(skill) == champion
    assert skill_revision(native / "pdf") == champion


def test_a_rollback_to_absence_removes_the_skill_from_every_target(tmp_path, monkeypatch):
    vault, skill = _vault(tmp_path, monkeypatch)
    _forbid_network(monkeypatch)
    publisher, native = _delivering(vault, tmp_path)
    publisher.process(_queue(skill).id)
    assert (native / "pdf").is_dir()

    P._snapshot_absence("pdf")
    P.rollback("pdf", P.ABSENT_REVISION, actor="admin")
    assert publisher.process(Q.publication_for_skill("pdf")["id"]) == "active"

    assert not skill.exists()
    assert not (native / "pdf").exists()


def _refuse_to_install(target, skill, source, revision):
    if target.kind == D.FILESYSTEM:
        raise OSError("the target is unavailable")
    return False


def test_a_failed_delivery_does_not_leave_an_active_release(tmp_path, monkeypatch):
    """The release is finished when every target holds it, not when the vault does. Marking it
    active on a partial delivery would report a change as live in places it never reached."""
    vault, skill = _vault(tmp_path, monkeypatch)
    _forbid_network(monkeypatch)
    publisher, native = _delivering(vault, tmp_path)
    receipt = _queue(skill)
    monkeypatch.setattr(D, "install", _refuse_to_install)

    with pytest.raises(RuntimeError, match="the target is unavailable"):
        publisher.process(receipt.id)

    record = Q.load_publication(receipt.id)
    assert record["state"] != "active"
    assert "the target is unavailable" in record["last_error"]
    assert record["delivery"]["claude"]["state"] == "failed"
    assert not (native / "pdf").exists()


def test_a_retried_publication_finishes_the_delivery_it_could_not_complete(tmp_path, monkeypatch):
    """The vault has already advanced by then, so the retry must deliver rather than decide the
    release is finished because the vault looks right."""
    vault, skill = _vault(tmp_path, monkeypatch)
    _forbid_network(monkeypatch)
    publisher, native = _delivering(vault, tmp_path)
    receipt = _queue(skill)
    real_install, refused = D.install, []

    def fail_the_first_delivery(target, name, source, revision):
        if target.kind == D.FILESYSTEM and not refused:
            refused.append(target.name)
            raise OSError("the target is unavailable")
        return real_install(target, name, source, revision)

    monkeypatch.setattr(D, "install", fail_the_first_delivery)
    with pytest.raises(RuntimeError):
        publisher.process(receipt.id)
    assert skill_revision(skill) == Q.load_publication(receipt.id)["candidate_revision"]

    assert publisher.process(receipt.id) == "active"
    assert skill_revision(native / "pdf") == skill_revision(skill)
    assert Q.load_publication(receipt.id)["delivery"]["claude"]["state"] == "delivered"
