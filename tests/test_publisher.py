import json
import os
import subprocess
from pathlib import Path

import pytest

from ingot.mcp_server.registry import load_skills, optimizable_components, skill_revision
from ingot.optimize import promote as P
from ingot.optimize import publication as Q
from ingot.optimize import publisher as W


def test_publisher_unit_uses_the_portable_managed_configuration():
    root = Path(__file__).resolve().parents[1]
    unit = (root / "ops/systemd/ingot-publisher.service").read_text()

    assert "EnvironmentFile=%h/.config/ingot/publisher.env" in unit
    assert "ExecStart=/usr/bin/python3 -m ingot.optimize.publisher --watch" in unit
    assert "Slancha" not in unit


def _git(path, *args, check=True):
    return subprocess.run(["git", "-C", str(path), *args], check=check,
                          capture_output=True, text=True).stdout.strip()


FORGE_REPOSITORY = "example/skills"


def _open(vault, remote, **kwargs):
    return W.VaultRepo.open(vault, remote="origin",
                            expected_remotes={str(Path(remote).resolve())}, **kwargs)


def _publisher(vault, remote, github=None):
    """A forge-backend publisher over a bare repository standing in for GitHub.

    `expected_remotes` is the configured repository's resolved URL. A test vault's origin is a
    filesystem path rather than a github.com URL, which is exactly the case the hardcoded pair of
    literals could not express."""
    backend = W.ForgeBackend(github if github is not None else FakeGitHub(),
                             repository=FORGE_REPOSITORY,
                             expected_remotes={str(Path(remote).resolve())})
    return W.Publisher(vault, backend=backend)


def _repositories(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    vault = tmp_path / "vault"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(vault)], check=True, capture_output=True)
    _git(vault, "config", "user.name", "Ingot Test")
    _git(vault, "config", "user.email", "ingot@test.invalid")
    skill = vault / "pdf"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: Merge PDFs.\n---\nold body\n")
    (vault / "registry.json").write_text(json.dumps({
        "pdf": {"disposition": "keep", "reason": "test"}
    }) + "\n")
    scripts = vault / "scripts"
    scripts.mkdir()
    (scripts / "validate.py").write_text("print('valid')\n")
    _git(vault, "add", ".")
    _git(vault, "commit", "-m", "Initial vault")
    _git(vault, "remote", "add", "origin", str(remote))
    _git(vault, "push", "-u", "origin", "main")
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
                   check=True)
    monkeypatch.setenv("INGOT_LIBRARY", str(vault))
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(vault))
    return remote, vault, skill


def _queue(skill):
    champion = optimizable_components(skill)
    challenger = {**champion, "body": "new body"}
    current = load_skills(skill.parent)[0]
    pending = {
        "skill": "pdf", "champion_components": champion,
        "challenger_components": challenger, "gate": {"promotable": True, "blocked": []},
        "evidence": {"champion": {"revision": current.revision},
                     "challenger": {"revision": skill_revision(skill, challenger)}},
    }
    P.save_pending("pdf", pending)
    return Q.queue_publication("pdf", pending, "admin", "promote")


class FakeGitHub:
    def __init__(self):
        self.merged = None

    def create_or_find(self, branch, publication_id):
        return 17

    def enable_auto_merge(self, pr):
        return None

    def merged_commit(self, pr):
        return self.merged


def _admin(remote, tmp_path):
    """A second clone standing in for whoever merges the vault pull request."""
    admin = tmp_path / "admin"
    if not admin.exists():
        subprocess.run(["git", "clone", str(remote), str(admin)], check=True, capture_output=True)
        _git(admin, "config", "user.name", "Vault Admin")
        _git(admin, "config", "user.email", "admin@test.invalid")
    return admin


def _merge(remote, tmp_path, branch):
    admin = _admin(remote, tmp_path)
    _git(admin, "fetch", "origin")
    _git(admin, "merge", "--ff-only", f"origin/{branch}")
    _git(admin, "push", "origin", "main")
    return _git(admin, "rev-parse", "HEAD")


def _publish(vault, publication_id, remote, tmp_path):
    """Drive one queued publication all the way through a merged vault commit."""
    github = FakeGitHub()
    publisher = _publisher(vault, remote, github)
    assert publisher.process(publication_id) == "awaiting_merge"
    github.merged = _merge(remote, tmp_path, Q.load_publication(publication_id)["branch"])
    assert publisher.process(publication_id) == "active"


def test_unmerged_publication_never_changes_served_skill(tmp_path, monkeypatch):
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    receipt = _queue(skill)
    github = FakeGitHub()
    publisher = _publisher(vault, remote, github)

    assert publisher.process(receipt.id) == "awaiting_merge"
    assert publisher.process(receipt.id) == "awaiting_merge"
    assert "old body" in (skill / "SKILL.md").read_text()
    assert P.pending_path("pdf").exists()


def test_merged_exact_revision_activates_and_consumes_pending(tmp_path, monkeypatch):
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    receipt = _queue(skill)
    github = FakeGitHub()
    publisher = _publisher(vault, remote, github)
    assert publisher.process(receipt.id) == "awaiting_merge"
    record = Q.load_publication(receipt.id)
    github.merged = _merge(remote, tmp_path, record["branch"])

    assert publisher.process(receipt.id) == "active"
    assert "new body" in (skill / "SKILL.md").read_text()
    assert not P.pending_path("pdf").exists()
    assert skill_revision(skill) == record["candidate_revision"]
    assert (P.revisions_dir() / "pdf" / record["expected_champion"]).is_dir()


def test_push_failure_preserves_champion_and_pending(tmp_path, monkeypatch):
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    receipt = _queue(skill)
    run = W._run

    def reject_push(command, *, cwd):
        if command[:2] == ["git", "push"]:
            raise RuntimeError("git push: rejected by test remote")
        return run(command, cwd=cwd)

    monkeypatch.setattr(W, "_run", reject_push)

    with pytest.raises(RuntimeError, match="git push"):
        _publisher(vault, remote).process(receipt.id)

    assert "old body" in (skill / "SKILL.md").read_text()
    assert P.pending_path("pdf").exists()
    assert Q.load_publication(receipt.id)["last_error"]


def test_retry_reuses_the_recorded_branch_after_push_failure(tmp_path, monkeypatch):
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    receipt = _queue(skill)
    run = W._run
    failures = 1

    def fail_once(command, *, cwd):
        nonlocal failures
        if command[:2] == ["git", "push"] and failures:
            failures -= 1
            raise RuntimeError("git push: transient failure")
        return run(command, cwd=cwd)

    monkeypatch.setattr(W, "_run", fail_once)
    publisher = _publisher(vault, remote)
    with pytest.raises(RuntimeError, match="transient failure"):
        publisher.process(receipt.id)

    assert publisher.process(receipt.id) == "awaiting_merge"
    record = Q.load_publication(receipt.id)
    assert record["branch"] == f"ingot/{receipt.id}"


def test_merged_commit_may_be_an_ancestor_of_a_newer_origin_main(tmp_path, monkeypatch):
    """An unrelated vault commit landing after the approved merge must not wedge the publication."""
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    receipt = _queue(skill)
    github = FakeGitHub()
    publisher = _publisher(vault, remote, github)
    assert publisher.process(receipt.id) == "awaiting_merge"
    github.merged = _merge(remote, tmp_path, Q.load_publication(receipt.id)["branch"])
    admin = _admin(remote, tmp_path)
    (admin / "NOTES.md").write_text("an unrelated vault edit\n")
    _git(admin, "add", "NOTES.md")
    _git(admin, "commit", "-m", "Unrelated vault commit")
    _git(admin, "push", "origin", "main")

    assert publisher.process(receipt.id) == "active"
    assert "new body" in (skill / "SKILL.md").read_text()
    assert (vault / "NOTES.md").exists()


def test_crash_after_fast_forward_finalizes_on_retry(tmp_path, monkeypatch):
    """The receipt is written after the fast-forward, so a crash between them leaves the approved
    revision already served. The retry must finalize rather than refuse the departed champion."""
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    receipt = _queue(skill)
    github = FakeGitHub()
    publisher = _publisher(vault, remote, github)
    assert publisher.process(receipt.id) == "awaiting_merge"
    github.merged = _merge(remote, tmp_path, Q.load_publication(receipt.id)["branch"])
    _git(vault, "fetch", "origin", "main")
    _git(vault, "merge", "--ff-only", "origin/main")          # the fast-forward that survived

    assert publisher.process(receipt.id) == "active"
    assert "new body" in (skill / "SKILL.md").read_text()
    assert not P.pending_path("pdf").exists()


def test_candidate_mismatch_never_consumes_pending_or_activates(tmp_path, monkeypatch):
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    receipt = _queue(skill)
    github = FakeGitHub()
    publisher = _publisher(vault, remote, github)
    assert publisher.process(receipt.id) == "awaiting_merge"
    github.merged = _merge(remote, tmp_path, Q.load_publication(receipt.id)["branch"])
    pending = P.load_pending("pdf")
    pending["evidence"]["challenger"]["revision"] = "f" * 64
    P.save_pending("pdf", pending)

    with pytest.raises(RuntimeError, match="pending review no longer matches"):
        publisher.process(receipt.id)

    assert P.pending_path("pdf").exists()
    assert Q.load_publication(receipt.id)["state"] == "awaiting_merge"
    assert "old body" in (skill / "SKILL.md").read_text()   # nothing was fast-forwarded either
    assert not (P.revisions_dir() / "pdf").exists()           # and nothing was snapshotted


def test_a_closed_vault_pull_request_is_refused_rather_than_reopened(tmp_path, monkeypatch):
    """Closing the vault pull request rejects the publication. Opening a second one for the same
    branch would overrule the person who closed it."""
    remote, vault, _ = _repositories(tmp_path, monkeypatch)
    monkeypatch.setattr(W, "_run", lambda command, *, cwd: json.dumps([
        {"number": 17, "state": "CLOSED"}]) if command[0] == "gh" else "")

    with pytest.raises(ValueError, match="closed without merging"):
        W.GitHub(vault, FORGE_REPOSITORY).create_or_find("ingot/abc123", "abc123")


def test_publication_replaces_a_stale_removal_entry_in_the_registry(tmp_path, monkeypatch):
    """A skill left marked for removal would land in the vault and then be dropped by the
    projection: the entry has to follow what the publication actually did."""
    remote, vault, _ = _repositories(tmp_path, monkeypatch)
    (vault / "registry.json").write_text(json.dumps({
        "pdf": {"disposition": "remove", "reason": "removed earlier"}}) + "\n")

    W.Publisher._register(vault, "pdf", present=True)

    assert json.loads((vault / "registry.json").read_text())["pdf"]["disposition"] == "keep"


def test_a_leftover_worktree_never_carries_unrelated_edits_into_the_pull_request(tmp_path,
                                                                                monkeypatch):
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    receipt = _queue(skill)
    workspace = Q.publications_dir() / "worktrees" / receipt.id
    workspace.mkdir(parents=True)
    (workspace / "STRAY.md").write_text("left behind by a killed run\n")

    publisher = _publisher(vault, remote)
    assert publisher.process(receipt.id) == "awaiting_merge"

    branch = Q.load_publication(receipt.id)["branch"]
    listed = _git(vault, "ls-tree", "-r", "--name-only", f"refs/heads/{branch}")
    assert "STRAY.md" not in listed
    assert "pdf/SKILL.md" in listed


def test_rollback_stays_inert_until_the_vault_merge_restores_it(tmp_path, monkeypatch):
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    champion = load_skills(vault)[0].revision
    _publish(vault, _queue(skill).id, remote, tmp_path)
    assert "new body" in (skill / "SKILL.md").read_text()

    result = P.rollback("pdf", champion, actor="admin")
    assert result.endswith("publishing to vault.")
    receipt = Q.publication_for_skill("pdf")
    github = FakeGitHub()
    publisher = _publisher(vault, remote, github)

    assert publisher.process(receipt["id"]) == "awaiting_merge"
    assert "new body" in (skill / "SKILL.md").read_text()     # inert until the merge lands

    github.merged = _merge(remote, tmp_path, Q.load_publication(receipt["id"])["branch"])
    assert publisher.process(receipt["id"]) == "active"
    assert "old body" in (skill / "SKILL.md").read_text()
    assert skill_revision(skill) == champion


def test_absence_rollback_removes_the_skill_only_after_merge(tmp_path, monkeypatch):
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    P._snapshot_absence("pdf")

    P.rollback("pdf", P.ABSENT_REVISION, actor="admin")
    receipt = Q.publication_for_skill("pdf")
    github = FakeGitHub()
    publisher = _publisher(vault, remote, github)

    assert publisher.process(receipt["id"]) == "awaiting_merge"
    assert skill.is_dir()

    github.merged = _merge(remote, tmp_path, Q.load_publication(receipt["id"])["branch"])
    assert publisher.process(receipt["id"]) == "active"
    assert not skill.exists()
    assert "pdf" not in json.loads((vault / "registry.json").read_text())


def test_absence_rollback_retries_after_the_branch_already_removed_the_skill(tmp_path, monkeypatch):
    """The second attempt starts from `origin/<branch>`, where the skill is already gone. Staging
    it by name is a fatal `git add` there, which wedged a real publication in the vault."""
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    P._snapshot_absence("pdf")
    P.rollback("pdf", P.ABSENT_REVISION, actor="admin")
    receipt = Q.publication_for_skill("pdf")

    class FailsOnce(FakeGitHub):
        calls = 0

        def create_or_find(self, branch, publication_id):
            FailsOnce.calls += 1
            if FailsOnce.calls == 1:
                raise RuntimeError("gh pr: transient failure after the push")
            return 17

    publisher = _publisher(vault, remote, FailsOnce())
    with pytest.raises(RuntimeError, match="transient failure"):
        publisher.process(receipt["id"])

    assert publisher.process(receipt["id"]) == "awaiting_merge"
    branch = Q.load_publication(receipt["id"])["branch"]
    assert "pdf/SKILL.md" not in _git(vault, "ls-tree", "-r", "--name-only", f"refs/heads/{branch}")


def test_a_vault_without_auto_merge_waits_for_a_human_instead_of_wedging(tmp_path, monkeypatch):
    """`laulpogan/skills` has auto-merge disabled. Treating that as fatal stranded an approval that
    was already sitting in an open pull request."""
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    receipt = _queue(skill)

    class NoAutoMerge(FakeGitHub):
        def enable_auto_merge(self, pr):
            raise RuntimeError("gh pr: GraphQL: Auto merge is not allowed for this repository")

    assert _publisher(vault, remote, NoAutoMerge()).process(receipt.id) == "awaiting_merge"

    record = Q.load_publication(receipt.id)
    assert record["pr"] == 17 and record["auto_merge"] is False
    assert "waiting on a human merge" in record["note"]
    assert record["last_error"] == ""
    assert "old body" in (skill / "SKILL.md").read_text()


def test_rollback_restores_a_file_the_displaced_revision_added(tmp_path, monkeypatch):
    """Components describe text the optimizer may rewrite, not the whole skill. Restoring the
    snapshot tree is what makes a rollback exact when a revision added a file."""
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    champion = load_skills(vault)[0].revision
    P._snapshot(skill, "pdf", champion)
    (skill / "extra.md").write_text("added by a later revision\n")
    _git(vault, "add", "pdf")
    _git(vault, "commit", "-m", "Add a bundled file")
    _git(vault, "push", "origin", "main")

    P.rollback("pdf", champion, actor="admin")
    receipt = Q.publication_for_skill("pdf")
    _publish(vault, receipt["id"], remote, tmp_path)

    assert not (skill / "extra.md").exists()
    assert skill_revision(skill) == champion


@pytest.mark.skipif(os.getuid() == 0, reason="root reads a mode-000 directory regardless")
def test_an_unreadable_receipt_store_is_reported_not_read_as_empty(tmp_path):
    """Path.glob swallows PermissionError, so a queue the publisher cannot list looks exactly like
    one with nothing in it: approvals pile up in the console and the publisher says nothing. This
    is the deployment failure where the console writes receipts as one user and the publisher runs
    as another."""
    store = tmp_path / "publications"
    store.mkdir()
    (store / "abc.json").write_text("{}")
    store.chmod(0o000)
    try:
        assert list(store.glob("*.json")) == []          # indistinguishable from empty
        blocked = W.unreadable_queue(store)
    finally:
        store.chmod(0o700)

    assert blocked and "cannot read the receipt store" in blocked
    assert W.unreadable_queue(tmp_path / "never-created") is None
    store.chmod(0o700)
    assert W.unreadable_queue(store) is None


def test_activation_retires_the_publication_branch(tmp_path, monkeypatch):
    """One branch per publication, kept forever, grows the vault's branch list without bound."""
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    receipt = _queue(skill)
    github = FakeGitHub()
    publisher = _publisher(vault, remote, github)
    assert publisher.process(receipt.id) == "awaiting_merge"
    branch = Q.load_publication(receipt.id)["branch"]
    github.merged = _merge(remote, tmp_path, branch)

    assert publisher.process(receipt.id) == "active"

    assert branch not in _git(vault, "branch", "--list", branch)
    assert not _git(vault, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    assert "new body" in (skill / "SKILL.md").read_text()


def test_a_branch_that_cannot_be_retired_leaves_the_publication_active(tmp_path, monkeypatch):
    """Cleanup runs after the receipt is durable, so a failure there must not turn a completed
    activation back into a retry."""
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    receipt = _queue(skill)
    github = FakeGitHub()
    publisher = _publisher(vault, remote, github)
    assert publisher.process(receipt.id) == "awaiting_merge"
    github.merged = _merge(remote, tmp_path, Q.load_publication(receipt.id)["branch"])
    run = W._run

    def refuse_delete(command, *, cwd):
        if "--delete" in command or command[:2] == ["git", "branch"]:
            raise RuntimeError("git push: the remote refused the deletion")
        return run(command, cwd=cwd)

    monkeypatch.setattr(W, "_run", refuse_delete)

    assert publisher.process(receipt.id) == "active"
    assert Q.load_publication(receipt.id)["state"] == "active"


def test_an_unrelated_vault_commit_does_not_block_the_next_publication(tmp_path, monkeypatch):
    """The vault has other writers. The served library is a mirror of vault main, so falling
    behind is normal — and refusing to publish until someone pulls by hand blocked the lane in
    production the first time anybody else committed."""
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    receipt = _queue(skill)
    admin = _admin(remote, tmp_path)
    (admin / "UNRELATED.md").write_text("someone else's vault commit\n")
    _git(admin, "add", "UNRELATED.md")
    _git(admin, "commit", "-m", "An unrelated vault commit")
    _git(admin, "push", "origin", "main")

    with pytest.raises(ValueError, match="HEAD must equal origin/main"):
        _open(vault, remote)                       # finalize still refuses to sync silently

    assert _publisher(vault, remote).process(receipt.id) == "awaiting_merge"
    assert (vault / "UNRELATED.md").exists()          # the mirror caught up
    assert "old body" in (skill / "SKILL.md").read_text()


def test_a_diverged_vault_is_never_synced(tmp_path, monkeypatch):
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    receipt = _queue(skill)
    (vault / "local.txt").write_text("a commit only this checkout has")
    _git(vault, "add", "local.txt")
    _git(vault, "commit", "-m", "Diverge")

    with pytest.raises(RuntimeError, match="diverged"):
        _publisher(vault, remote).process(receipt.id)


@pytest.mark.parametrize("fault", ["dirty", "detached", "wrong_remote", "diverged"])
def test_repository_guard_fails_before_publication_writes(tmp_path, monkeypatch, fault):
    remote, vault, _ = _repositories(tmp_path, monkeypatch)
    if fault == "dirty":
        (vault / "dirty.txt").write_text("dirty")
    elif fault == "detached":
        _git(vault, "checkout", "--detach")
    elif fault == "wrong_remote":
        _git(vault, "remote", "set-url", "origin", str(tmp_path / "wrong.git"))
    else:
        (vault / "local.txt").write_text("local")
        _git(vault, "add", "local.txt")
        _git(vault, "commit", "-m", "Diverge")

    with pytest.raises(ValueError):
        _open(vault, remote)


def test_polling_an_unmerged_publication_does_not_touch_the_vault(tmp_path, monkeypatch):
    """`watch` polls every few seconds. Fetching and re-validating the checkout on each poll puts a
    network round trip -- and a failure mode -- in front of a question `gh` already answers, and it
    made an unrelated dirty working tree fail a receipt that was simply still waiting."""
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    receipt = _queue(skill)
    publisher = _publisher(vault, remote)
    assert publisher.process(receipt.id) == "awaiting_merge"
    (vault / "someone-is-editing.txt").write_text("an unrelated local edit")

    run = W._run
    touched = []

    def record(command, *, cwd):
        touched.append(command)
        return run(command, cwd=cwd)

    monkeypatch.setattr(W, "_run", record)

    assert publisher.process(receipt.id) == "awaiting_merge"

    assert not [command for command in touched if command[:2] == ["git", "fetch"]]
    assert Q.load_publication(receipt.id)["last_error"] == ""


def test_a_merge_that_just_landed_is_not_refused_as_missing(tmp_path, monkeypatch):
    """The ancestry check runs against whatever `origin/main` this checkout last saw. Without a
    fetch of its own it would refuse the one outcome the lane is waiting for."""
    remote, vault, skill = _repositories(tmp_path, monkeypatch)
    receipt = _queue(skill)
    github = FakeGitHub()
    publisher = _publisher(vault, remote, github)
    assert publisher.process(receipt.id) == "awaiting_merge"
    github.merged = _merge(remote, tmp_path, Q.load_publication(receipt.id)["branch"])
    assert not W.VaultRepo(vault, remote="origin").contains(github.merged)   # not fetched yet

    assert publisher.process(receipt.id) == "active"
    assert "new body" in (skill / "SKILL.md").read_text()
