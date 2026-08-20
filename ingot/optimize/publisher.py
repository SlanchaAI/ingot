"""Publish approved skill bytes through Git, then finalize only the authorized vault revision.

Two backends sit behind one publication contract. `local` is the default: the vault is a Git
repository on this machine, nothing leaves it, and the approval that queued the receipt is the only
gate. `forge` is opt-in and preserves the GitHub lane — push, pull request, merge — for deployments
that want the vault mirrored and the activation anchored somewhere a local administrator cannot
rewrite. The backend is always selected explicitly; a vault that happens to have an `origin` must
never start opening pull requests on its own.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from ingot import delivery
from ingot.mcp_server.registry import skill_revision, write_components, write_skill_md
from ingot.optimize import promote
from ingot.optimize import publication
from ingot.optimize import tree


BACKENDS = ("local", "forge")
DEFAULT_BACKEND = "local"
DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"
PUBLISHER_IDENTITY = ("-c", "user.name=Ingot Publisher", "-c", "user.email=ingot@local.invalid")


class ConfigurationError(ValueError):
    """A publisher that must refuse to start rather than queue approvals it can never publish."""


def _run(command: list[str], *, cwd: Path) -> str:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        label = " ".join(command[:2])
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "command failed"
        raise RuntimeError(f"{label}: {detail}")
    return result.stdout.strip()


def _forge_urls(repository: str) -> set[str]:
    """Every spelling a checkout of one GitHub repository legitimately uses for its remote."""
    return {f"https://github.com/{repository}.git", f"https://github.com/{repository}",
            f"git@github.com:{repository}.git", f"git@github.com:{repository}",
            f"ssh://git@github.com/{repository}.git", f"ssh://git@github.com/{repository}"}


class VaultRepo:
    """The vault checkout, which is also the served checkout.

    `remote` is None in local mode, where this repository is the whole history there is: no origin
    to verify, nothing to fetch, and `main` is the only authority."""

    def __init__(self, path: Path, *, remote: str | None = None, branch: str = DEFAULT_BRANCH):
        self.path = path
        self.remote = remote
        self.branch = branch

    @property
    def base(self) -> str:
        """The ref publications are cut from and fast-forwarded onto."""
        return f"{self.remote}/{self.branch}" if self.remote else self.branch

    def git(self, *args: str) -> str:
        return _run(["git", *args], cwd=self.path)

    def _is_ancestor(self, commit: str, of: str) -> bool:
        result = subprocess.run(["git", "merge-base", "--is-ancestor", commit, of],
                                cwd=self.path, capture_output=True)
        return result.returncode == 0

    def contains(self, commit: str) -> bool:
        """Whether the base ref already carries this commit, directly or beneath a later one."""
        return self._is_ancestor(commit, self.base)

    def start_point(self, branch: str) -> str:
        """Where this publication's worktree is cut from.

        An existing publication branch is reused so a retry is idempotent — the absence rollback
        depends on it, because its second attempt must start from a tree where the skill is already
        gone. In local mode a branch that no longer descends from `main` (someone committed to the
        vault directly in between) can never fast-forward, so it is abandoned and recut from `main`
        rather than left to wedge the receipt on every retry. The publication is re-materialized
        against the new champion and refused if it no longer matches."""
        if self.remote:
            found = self.git("ls-remote", "--heads", self.remote, f"refs/heads/{branch}")
            return f"{self.remote}/{branch}" if found else self.base
        exists = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                                cwd=self.path, capture_output=True).returncode == 0
        return branch if exists and self._is_ancestor(self.base, branch) else self.base

    @classmethod
    def open(cls, path: Path, *, remote: str | None = None, expected_remotes: set[str] | None = None,
             branch: str = DEFAULT_BRANCH, allow_behind: bool = False,
             sync: bool = False) -> "VaultRepo":
        """Open the served vault checkout, refusing anything the publisher must not build on.

        `sync` fast-forwards a checkout that is merely behind its remote. The vault has other
        writers — a person committing directly, another machine — and the served library is by
        definition a mirror of vault `main`, so falling behind is normal and must not block
        publishing. Only `_prepare` may sync: in `_finalize` the fast-forward is the activation step
        itself, and syncing early would discard the champion bytes before they are snapshotted as
        the rollback target. In local mode there is no remote, so both are no-ops."""
        resolved = path.expanduser().resolve(strict=True)
        repo = cls(resolved, remote=remote, branch=branch)
        if remote is not None and expected_remotes is not None:
            if repo.git("remote", "get-url", remote) not in expected_remotes:
                raise ValueError(f"vault {remote} is not the configured vault repository")
        try:
            head = repo.git("symbolic-ref", "--short", "HEAD")
        except RuntimeError as exc:
            raise ValueError("vault checkout is detached") from exc
        if head != branch:
            raise ValueError(f"vault checkout must remain on {branch}")
        if repo.git("status", "--porcelain"):
            raise ValueError("vault checkout is dirty")
        if remote is None:
            return repo
        repo.git("fetch", remote, branch)
        head_commit = repo.git("rev-parse", "HEAD")
        upstream = repo.git("rev-parse", repo.base)
        if head_commit != upstream:
            if not repo._is_ancestor(head_commit, repo.base):
                raise ValueError(f"vault {branch} diverged from {repo.base}")
            if sync:
                repo.git("merge", "--ff-only", repo.base)
            elif not allow_behind:
                raise ValueError(f"vault HEAD must equal {repo.base}")
        return repo


class LocalBackend:
    """The default. The vault is a local Git repository and no network is involved at any point.

    There is no `awaiting_merge` state: the commit is authorized the moment it exists, because the
    human gate is the approval that queued the receipt, not a second review of the same bytes."""

    name = "local"
    remote = None
    expected_remotes = None

    def __init__(self, *, branch: str = DEFAULT_BRANCH):
        self.branch = branch

    def authorize(self, repo: VaultRepo, record: dict, branch: str,
                  workspace: Path | None = None) -> str | None:
        return repo.git("rev-parse", f"refs/heads/{branch}")

    def advance(self, repo: VaultRepo, record: dict, commit: str) -> None:
        """Fast-forward only. A vault whose `main` moved under the publication is a refusal, never
        a rebase, a merge commit, or a reset: the operator resolves it and the receipt retries."""
        repo.git("merge", "--ff-only", commit)

    def retire(self, repo: VaultRepo, record: dict) -> None:
        _retire(repo, record, (("branch", "-D", record.get("branch", "")),))


class GitHub:
    """`gh` against the vault checkout, whose authenticated host credentials this process reuses."""

    def __init__(self, vault_dir: Path, repository: str, *, base: str = DEFAULT_BRANCH):
        self.vault_dir = vault_dir
        self.repository = repository
        self.base = base

    def _json(self, args: list[str]):
        output = _run(["gh", *args], cwd=self.vault_dir)
        return json.loads(output) if output else None

    def create_or_find(self, branch: str, publication_id: str) -> int:
        found = self._json(["pr", "list", "--repo", self.repository, "--head", branch,
                            "--state", "all", "--json", "number,state"]) or []
        if found:
            # Someone closing the vault pull request has rejected the publication. Opening a second
            # one for the same branch would overrule that, so this refuses and leaves the reason on
            # the receipt instead.
            if found[0].get("state") == "CLOSED":
                raise ValueError(f"the vault pull request for {branch} was closed without merging")
            return int(found[0]["number"])
        output = _run(["gh", "pr", "create", "--repo", self.repository, "--base", self.base,
                       "--head", branch, "--title", f"Publish Ingot approval {publication_id}",
                       "--body", f"Evidence-gated publication `{publication_id}`."],
                      cwd=self.vault_dir)
        return int(output.rstrip("/").split("/")[-1])

    def enable_auto_merge(self, pr: int) -> None:
        _run(["gh", "pr", "merge", str(pr), "--repo", self.repository, "--auto", "--squash"],
             cwd=self.vault_dir)

    def merged_commit(self, pr: int) -> str | None:
        data = self._json(["pr", "view", str(pr), "--repo", self.repository,
                           "--json", "state,mergeCommit"])
        if not data or data.get("state") != "MERGED":
            return None
        return (data.get("mergeCommit") or {}).get("oid")


class ForgeBackend:
    """Opt-in. Publication authority is a merged pull request in a configured Git forge."""

    name = "forge"

    def __init__(self, github=None, *, repository: str, remote: str = DEFAULT_REMOTE,
                 branch: str = DEFAULT_BRANCH, expected_remotes: set[str] | None = None):
        if not repository:
            raise ConfigurationError("the forge backend requires a repository "
                                     "(INGOT_FORGE_REPOSITORY)")
        self.repository = repository
        self.remote = remote
        self.branch = branch
        self.expected_remotes = expected_remotes or _forge_urls(repository)
        self.github = github

    def _forge(self, repo: VaultRepo) -> GitHub:
        if self.github is None:
            self.github = GitHub(repo.path, self.repository, base=self.branch)
        return self.github

    def authorize(self, repo: VaultRepo, record: dict, branch: str,
                  workspace: Path | None = None) -> str | None:
        """With a workspace, submit the branch for authority; without one, ask whether it landed.

        A vault that does not allow auto-merge is not a failure: the pull request is open and a
        human merging it is the gate this whole lane exists for. Wedging the receipt here would
        strand an approval that is already waiting on GitHub, and nothing downstream acts until the
        merge is visible, so the guarantee is unchanged either way."""
        github = self._forge(repo)
        if workspace is not None:
            _run(["git", "push", self.remote, f"HEAD:refs/heads/{branch}"], cwd=workspace)
            pr = github.create_or_find(branch, record["id"])
            publication.update_publication(record["id"], branch=branch, pr=pr)
            auto_merge, note = True, ""
            try:
                github.enable_auto_merge(pr)
            except RuntimeError as exc:
                auto_merge, note = False, f"auto-merge unavailable, waiting on a human merge: {exc}"
            publication.update_publication(record["id"], pr=pr, auto_merge=auto_merge, note=note)
            return None
        merged = github.merged_commit(int(record["pr"]))
        if not merged:
            return None
        # Fetch here rather than relying on the caller having opened the vault: a merge that landed
        # a second ago is not an ancestor of a stale `origin/main`, and refusing it would fail the
        # receipt for the one outcome this lane is waiting for.
        repo.git("fetch", self.remote, self.branch)
        if not repo.contains(merged):
            raise ValueError(f"the approved merge commit is not part of {repo.base}")
        return merged

    def advance(self, repo: VaultRepo, record: dict, commit: str) -> None:
        # The authorized commit is already on the base ref, verified above, so the served checkout
        # advances by fast-forwarding onto it rather than onto the branch.
        repo.git("merge", "--ff-only", repo.base)

    def retire(self, repo: VaultRepo, record: dict) -> None:
        branch = record.get("branch", "")
        _retire(repo, record, (("push", self.remote, "--delete", branch), ("branch", "-D", branch)))


def _retire(repo: VaultRepo, record: dict, commands) -> None:
    """Drop the publication branch once its commit is served.

    Every publication opens one, so leaving them behind grows the vault's branch list without
    bound. This runs after the receipt is durable and never raises: the publication is already
    active, and an uncleaned branch must not turn a completed activation into a retry."""
    if not record.get("branch"):
        return
    for command in commands:
        try:
            repo.git(*command)
        except RuntimeError as exc:
            print(f"[publisher] {record['id']}: could not remove {record['branch']}: {exc}",
                  flush=True)


class Publisher:
    def __init__(self, vault_dir: Path, *, backend=None, targets=None):
        self.vault_dir = vault_dir.expanduser().resolve()
        self.backend = backend if backend is not None else LocalBackend()
        # An unconfigured publisher delivers to the vault and nowhere else, which is what every
        # deployment before delivery targets existed already did.
        self.targets = (tuple(targets) if targets is not None
                        else delivery.parse_targets("", vault=self.vault_dir))

    def _repo(self) -> VaultRepo:
        """The vault checkout, unvalidated. For questions that do not build on it."""
        return VaultRepo(self.vault_dir, remote=self.backend.remote, branch=self.backend.branch)

    def _open(self, **kwargs) -> VaultRepo:
        return VaultRepo.open(self.vault_dir, remote=self.backend.remote,
                              expected_remotes=self.backend.expected_remotes,
                              branch=self.backend.branch, **kwargs)

    @staticmethod
    def _branch(record: dict) -> str:
        return f"ingot/{record['id']}"

    def _workspace(self, record: dict) -> Path:
        return publication.publications_dir() / "worktrees" / record["id"]

    @staticmethod
    def _register(root: Path, skill: str, *, present: bool) -> None:
        """Keep the vault registry in step with what the publication adds or removes.

        A stale entry left by an earlier removal would otherwise survive: the skill would land in
        the vault still marked for removal, and the projection would drop what was just approved. A
        curated `keep` entry is left exactly as the operator wrote it."""
        path = root / "registry.json"
        registry = json.loads(path.read_text(encoding="utf-8"))
        if present:
            entry = registry.get(skill)
            if not isinstance(entry, dict) or entry.get("disposition") != "keep":
                registry[skill] = {"disposition": "keep", "reason": "Approved through Ingot."}
        else:
            registry.pop(skill, None)
        path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")

    def _materialize_rollback(self, root: Path, record: dict) -> None:
        """Restore a stored snapshot wholesale.

        Writing the recorded components back would only restore text the optimizer can rewrite: a
        file the displaced revision added, or any non-text file, would survive the rollback and the
        revision check below would then refuse it. The snapshot is a complete copy of the skill, so
        replacing the directory with it is both simpler and exact."""
        skill = root / record["skill"]
        if skill.is_dir():
            shutil.rmtree(skill)
        if record["candidate_revision"] == promote.ABSENT_REVISION:
            self._register(root, record["skill"], present=False)
            return
        source = promote._rollback_source(record["skill"], record["candidate_revision"])
        shutil.copytree(source, skill, symlinks=True)
        self._register(root, record["skill"], present=True)
        if skill_revision(skill) != record["candidate_revision"]:
            raise ValueError("restored vault package does not match the approved revision")

    def _materialize(self, root: Path, record: dict) -> None:
        if record["action"] == "rollback":
            return self._materialize_rollback(root, record)
        skill = root / record["skill"]
        components = record["components"]
        candidate_tree = record.get("tree")
        if record["kind"] == "creation":
            if skill.is_dir() and skill_revision(skill) == record["candidate_revision"]:
                return
            if skill.exists() or skill.is_symlink():
                raise ValueError(f"skill '{record['skill']}' already exists in the vault")
            if candidate_tree:
                # Every file the operator ingested, byte for byte, verified against the receipt on
                # the way in. `write_components` is deliberately not called afterwards: it would
                # rewrite each text file from a decoded copy, and a decoded copy is exactly what
                # this exists to stop the vault from serving.
                tree.materialize_creation(candidate_tree, components, record["skill"], skill)
                self._register(root, record["skill"], present=True)
                if skill_revision(skill) != record["candidate_revision"]:
                    raise ValueError(
                        "materialized vault package does not match the approved revision")
                return
            skill.mkdir(parents=True)
            try:
                metadata = json.loads(components.get("frontmatter", "{}"))
            except (TypeError, ValueError) as exc:
                raise ValueError("creation frontmatter is not valid JSON") from exc
            metadata["name"] = record["skill"]
            metadata["description"] = components["description"]
            write_skill_md(skill / "SKILL.md", metadata, components["body"])
            self._register(root, record["skill"], present=True)
        else:
            if not skill.is_dir():
                raise ValueError(f"skill '{record['skill']}' is absent from the vault")
            current = skill_revision(skill)
            if current == record["candidate_revision"]:
                return
            if current != record["expected_champion"]:
                raise ValueError("vault champion does not match the approved revision")
        write_components(skill, components)
        if skill_revision(skill) != record["candidate_revision"]:
            raise ValueError("materialized vault package does not match the approved revision")

    def _prepare(self, record: dict) -> str:
        repo = self._open(sync=True)
        branch = record.get("branch") or self._branch(record)
        publication.update_publication(record["id"], state="publishing", branch=branch,
                                       attempts=record.get("attempts", 0) + 1, last_error="")
        workspace = self._workspace(record)
        workspace.parent.mkdir(parents=True, exist_ok=True)
        start = repo.start_point(branch)
        # A run killed mid-publication leaves its worktree behind. Reusing it would let whatever it
        # holds — a half-written materialization, an unrelated edit — ride into the publication,
        # so every attempt starts from a worktree cut fresh from the recorded start point.
        if workspace.exists():
            try:
                repo.git("worktree", "remove", "--force", str(workspace))
            except RuntimeError:
                shutil.rmtree(workspace, ignore_errors=True)
                repo.git("worktree", "prune")
        repo.git("worktree", "add", "-B", branch, str(workspace), start)
        self._materialize(workspace, record)
        _run([sys.executable, "scripts/validate.py"], cwd=workspace)
        # Stage the whole worktree rather than naming the skill: a retry of an absence rollback
        # starts from the publication branch, where the skill directory is already gone, and naming
        # a pathspec that matches nothing is a fatal `git add`. The worktree is cut fresh from the
        # start ref and `_materialize` is its only writer, so there is nothing else in it to stage.
        _run(["git", "add", "-A", "."], cwd=workspace)
        # An empty diff is a no-op, not an error: re-publishing a revision the vault already
        # carries must converge on the existing branch tip.
        if _run(["git", "status", "--porcelain"], cwd=workspace):
            _run(["git", *PUBLISHER_IDENTITY, "commit", "-m",
                  f"Publish {record['skill']} via Ingot {record['id']}"], cwd=workspace)
        authorized = self.backend.authorize(repo, record, branch, workspace)
        repo.git("worktree", "remove", "--force", str(workspace))
        branch_commit = repo.git("rev-parse", f"refs/heads/{branch}")
        if authorized is None:
            publication.update_publication(record["id"], state="awaiting_merge", branch=branch,
                                           branch_commit=branch_commit, last_error="")
            return "awaiting_merge"
        # The local backend has no external gate to wait on, so one pass runs both halves. A crash
        # in between leaves the receipt at `publishing` with the branch committed, and the next
        # pass re-enters here, finds the branch, re-authorizes, and finalizes.
        publication.update_publication(record["id"], branch=branch, branch_commit=branch_commit,
                                       last_error="")
        return self._finalize(publication.load_publication(record["id"]))

    def _serves(self, record: dict, revision: str) -> bool:
        """Whether the served checkout is exactly at one recorded revision. Absence is a revision:
        a creation's champion and an absence rollback's candidate are both 'no directory at all'."""
        skill = self.vault_dir / record["skill"]
        if revision == promote.ABSENT_REVISION:
            return not skill.exists()
        return skill.is_dir() and skill_revision(skill) == revision

    def _deliver(self, record: dict) -> None:
        """Install the approved revision at every configured target.

        Runs after the vault serves the candidate and before the receipt is marked active, so a
        target that cannot be written leaves a release that retries rather than one that claims to
        be finished. Each target's outcome is recorded separately: a deployment with two targets
        where one failed must be able to say which one.

        The source is the vault's own copy, already checked against the receipt by `_serves`. A
        failure raises, and `process` records it on the receipt as `last_error`."""
        skill, revision = record["skill"], record["candidate_revision"]
        source = None if revision == promote.ABSENT_REVISION else self.vault_dir / skill
        if source is not None and not source.is_dir():
            raise ValueError(f"the vault does not hold '{skill}' to deliver")
        delivered = dict(record.get("delivery") or {})
        for target in self.targets:
            entry = {"kind": target.kind, "root": str(target.root), "at": int(time.time())}
            try:
                delivery.install(target, skill, source, revision)
            except Exception as exc:
                delivered[target.name] = {**entry, "state": "failed", "error": str(exc),
                                          "revision": delivery.observed(target, skill)}
                publication.update_publication(record["id"], delivery=delivered)
                raise
            delivered[target.name] = {**entry, "state": "delivered", "revision": revision}
        publication.update_publication(record["id"], delivery=delivered)

    def _finalize(self, record: dict) -> str:
        branch = record.get("branch") or self._branch(record)
        # Ask whether authority exists before validating the checkout. A receipt waiting on a forge
        # merge is polled every few seconds and opening the vault fetches, so validating first put
        # a network round trip -- and a failure mode -- in front of a question that needs neither.
        authorized = self.backend.authorize(self._repo(), record, branch)
        if not authorized:
            return "awaiting_merge"
        repo = self._open(allow_behind=True)
        # Check the review slot before anything is snapshotted or fast-forwarded. Validating it
        # afterwards would leave a receipt that no longer matches its review having already moved
        # the served bytes, which is the one thing this lane exists to prevent. Only an approval
        # owns a review slot: a rollback publishes a stored snapshot and must leave an unrelated
        # pending challenger for that skill reviewable.
        pending = promote.load_pending(record["skill"]) if record["action"] == "promote" else None
        if pending is not None:
            recorded = ((pending.get("evidence") or {}).get("challenger") or {}).get("revision")
            if recorded != record["candidate_revision"]:
                raise ValueError("pending review no longer matches the publication")
        # A crash between the fast-forward and this receipt leaves the approved revision already
        # served. Re-snapshotting and re-merging then would refuse a champion that is legitimately
        # gone, so a checkout already at the candidate skips straight to finalizing the receipt.
        if not self._serves(record, record["candidate_revision"]):
            if not self._serves(record, record["expected_champion"]):
                raise ValueError("served champion changed before vault activation")
            if record["expected_champion"] == promote.ABSENT_REVISION:
                promote._snapshot_absence(record["skill"])
            else:
                promote._snapshot(self.vault_dir / record["skill"], record["skill"],
                                  record["expected_champion"])
            self.backend.advance(repo, record, authorized)
            if not self._serves(record, record["candidate_revision"]):
                raise ValueError("the fast-forwarded vault does not serve the approved revision")
        self._deliver(record)
        if pending is not None:
            promote.pending_path(record["skill"]).unlink()
        promote._audit_best_effort(record["action"] if record["action"] == "rollback" else "approve",
                                   record["skill"], record["candidate_revision"], record["actor"])
        publication.update_publication(record["id"], state="active", merged_commit=authorized,
                                       activated=int(time.time()), last_error="")
        self.backend.retire(repo, record)
        return "active"

    def process(self, publication_id: str) -> str:
        record = publication.load_publication(publication_id)
        if not record:
            raise ValueError(f"unknown publication: {publication_id}")
        try:
            # The queue validates both of these when it writes a receipt. Re-checking them here
            # keeps a receipt that was edited or corrupted on disk from steering a filesystem path
            # or reaching an activation branch it was never approved for.
            promote.check_slug(record["skill"])
            if record.get("action") not in publication.ACTIONS:
                raise ValueError(f"invalid publication action: {record.get('action')!r}")
            if record["state"] in {"approved_publishing", "publishing"}:
                return self._prepare(record)
            if record["state"] == "awaiting_merge":
                return self._finalize(record)
            return record["state"]
        except Exception as exc:
            current = publication.load_publication(publication_id) or record
            publication.update_publication(
                publication_id, attempts=current.get("attempts", 0) + 1, last_error=str(exc))
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"publication {publication_id}: {exc}") from exc


@dataclass(frozen=True)
class PublisherConfig:
    """What the publisher was told to be, before anything checks whether it can be."""

    backend: str
    vault_dir: Path
    forge_repository: str | None = None
    forge_remote: str = DEFAULT_REMOTE
    forge_branch: str = DEFAULT_BRANCH
    delivery_targets: tuple = field(default=())
    warnings: tuple[str, ...] = field(default=())

    def build(self) -> Publisher:
        targets = self.delivery_targets or None
        if self.backend == "forge":
            return Publisher(self.vault_dir, targets=targets, backend=ForgeBackend(
                repository=self.forge_repository, remote=self.forge_remote,
                branch=self.forge_branch))
        return Publisher(self.vault_dir, targets=targets, backend=LocalBackend())


FORGE_KEYS = ("INGOT_FORGE_REPOSITORY", "INGOT_FORGE_REMOTE", "INGOT_FORGE_BRANCH")


def load_config(env: dict | None = None, *, backend: str | None = None,
                vault: Path | None = None) -> PublisherConfig:
    """Explicit argument, then environment. There is no fallback to a writable default path.

    A publisher that guesses its vault would happily own the demo directory, which is the one
    outcome managed mode exists to prevent."""
    env = os.environ if env is None else env
    chosen = backend or env.get("INGOT_PUBLISH_BACKEND") or DEFAULT_BACKEND
    if chosen not in BACKENDS:
        raise ConfigurationError(f"unknown publication backend {chosen!r}; "
                                 f"expected one of {', '.join(BACKENDS)}")
    warnings = []
    path = vault or env.get("INGOT_VAULT_PATH") or env.get("VAULT_DIR")
    if not path:
        raise ConfigurationError("no vault configured; set INGOT_VAULT_PATH to the checkout this "
                                 "publisher owns, or pass --vault")
    if not vault and not env.get("INGOT_VAULT_PATH") and env.get("VAULT_DIR"):
        warnings.append("VAULT_DIR is the pre-backend name for INGOT_VAULT_PATH; prefer the latter")
    repository = env.get("INGOT_FORGE_REPOSITORY")
    if chosen == "local":
        # A half-finished forge configuration under the local backend looks active and is not. Say
        # so rather than letting someone believe their pull requests are being opened.
        stray = [key for key in FORGE_KEYS if env.get(key)]
        if stray:
            warnings.append(f"{', '.join(stray)} set but the backend is local; forge settings are "
                            f"inert until INGOT_PUBLISH_BACKEND=forge")
    elif not repository:
        raise ConfigurationError("the forge backend requires INGOT_FORGE_REPOSITORY=owner/repo")
    vault_dir = Path(path).expanduser()
    try:
        targets = delivery.parse_targets(env.get(delivery.TARGETS) or "", vault=vault_dir)
    except ValueError as exc:
        raise ConfigurationError(f"{delivery.TARGETS}: {exc}") from exc
    return PublisherConfig(
        backend=chosen, vault_dir=Path(path), forge_repository=repository,
        forge_remote=env.get("INGOT_FORGE_REMOTE") or DEFAULT_REMOTE,
        forge_branch=env.get("INGOT_FORGE_BRANCH") or DEFAULT_BRANCH,
        delivery_targets=targets, warnings=tuple(warnings))


def validate(config: PublisherConfig) -> Publisher:
    """Refuse to start rather than fail on the first approval.

    An approval queued against a publisher that cannot publish is the stalled-lane failure this
    deployment has already hit once: the console reports the change accepted, the receipt sits at
    `approved_publishing` forever, and nothing says why."""
    publisher = config.build()
    if config.backend == "forge":
        if not shutil.which("gh"):
            raise ConfigurationError("the forge backend needs the GitHub CLI (`gh`) on PATH")
        try:
            _run(["gh", "auth", "status"], cwd=Path.cwd())
        except RuntimeError as exc:
            raise ConfigurationError(f"`gh` is not authenticated: {exc}") from exc
        try:
            _run(["gh", "repo", "view", config.forge_repository, "--json", "name"], cwd=Path.cwd())
        except RuntimeError as exc:
            raise ConfigurationError(
                f"the configured forge repository {config.forge_repository!r} does not "
                f"resolve: {exc}") from exc
    try:
        publisher._open(allow_behind=True)
    except (ValueError, OSError) as exc:
        raise ConfigurationError(f"vault at {config.vault_dir}: {exc}") from exc
    for target in config.delivery_targets:
        if target.kind != delivery.FILESYSTEM:
            continue
        # Created here rather than on the first approval. A delivery root that cannot be made or
        # written strands a change that a human has already approved, and this is the one place
        # that can say so before anybody approves anything.
        try:
            target.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigurationError(
                f"delivery target {target.name!r}: cannot create {target.root}: {exc}") from exc
        if not os.access(target.root, os.W_OK | os.X_OK):
            raise ConfigurationError(f"delivery target {target.name!r}: {target.root} is not "
                                     f"writable by uid {os.getuid()}")
    validator = config.vault_dir / "scripts" / "validate.py"
    if not validator.is_file():
        raise ConfigurationError(
            f"the vault has no validator at {validator}; every publication runs it before "
            f"committing, so a missing one is a configuration error and not a silent skip "
            f"(`ingot vault init {config.vault_dir}` writes a default)")
    return publisher


def unreadable_queue(directory: Path) -> str | None:
    """Why the receipt store cannot be read, or None.

    `Path.glob` swallows `PermissionError`, so a queue this process cannot list is indistinguishable
    from an empty one: approvals pile up in the console while the publisher reports nothing at all.
    That is the exact shape of the deployment failure on the host — receipts written by a container
    running as root, read by a publisher running as an ordinary user."""
    if not directory.exists():
        return None
    if not os.access(directory, os.R_OK | os.X_OK):
        return (f"cannot read the receipt store at {directory} as uid {os.getuid()}; approvals will "
                f"queue and never publish (the console writes it at mode 0700, so both must run as "
                f"the same user)")
    return None


def watch(publisher: Publisher, interval: float = 5.0) -> None:
    reported = None
    while True:
        blocked = unreadable_queue(publication.publications_dir())
        if blocked != reported:      # report each transition once, not every poll
            print(f"[publisher] {blocked}" if blocked else "[publisher] receipt store readable again",
                  flush=True)
            reported = blocked
        if not blocked:
            for path in sorted(publication.publications_dir().glob("*.json")):
                try:
                    publisher.process(path.stem)
                except Exception as exc:
                    print(f"[publisher] {path.stem}: {exc}", flush=True)
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ingot.optimize.publisher",
        description="The one writer of the served skill library.")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--backend", choices=BACKENDS, default=None,
                        help="publication backend (default: $INGOT_PUBLISH_BACKEND, else local)")
    parser.add_argument("--vault", type=Path, default=None,
                        help="the vault checkout this publisher owns "
                             "(default: $INGOT_VAULT_PATH)")
    parser.add_argument("publication_id", nargs="?")
    args = parser.parse_args(argv)
    try:
        config = load_config(backend=args.backend, vault=args.vault)
        for warning in config.warnings:
            print(f"[publisher] warning: {warning}", flush=True)
        publisher = validate(config)
    except ConfigurationError as exc:
        print(f"[publisher] refusing to start: {exc}", file=sys.stderr, flush=True)
        return 2
    print(f"[publisher] backend={config.backend} vault={config.vault_dir}", flush=True)
    for target in config.delivery_targets:
        print(f"[publisher] delivering to {target.name} ({target.kind}) at {target.root}",
              flush=True)
    if args.watch:
        watch(publisher)
        return 0
    if not args.publication_id:
        parser.error("publication_id is required without --watch")
    print(publisher.process(args.publication_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
