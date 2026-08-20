"""Where an approved revision is installed once the vault already carries it.

The vault is the publication authority and the managed-MCP library at the same time: the publisher
commits into it, fast-forwards it, and every other service mounts it read-only. That covers agents
that load skills through Ingot's MCP server. It does not cover an agent that reads a native skill
directory on disk, which is most of them.

A delivery target closes that gap without opening a second way to approve anything. Targets are
configured on the publisher, never named in a receipt's identity, so what is approved does not
depend on where a particular deployment happens to install it. Delivery runs *after* the vault
serves the approved revision and *before* the receipt is marked active, which is what makes a
failed delivery a retryable release rather than a finished one.

Two kinds:

- `managed-mcp` is the vault itself. Delivering to it is a deliberate no-op -- the publication
  commit and the fast-forward already put the bytes there, and a second writer in the vault is the
  one thing this control plane exists to prevent. It appears in the target list so it has a name,
  a status, and a per-target line on the receipt like any other destination.
- `filesystem` copies the skill directory out of the vault into a configured root.

The source of every delivery is the vault checkout, already verified to be at the approved
revision. Nothing here re-derives the bytes from components or from a staged tree: a second
materialization is a second thing that can disagree with the first.
"""
from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from ingot.mcp_server.registry import SLUG_RE, skill_revision
from ingot.optimize import promote

DELIVERY_SCHEMA = "ingot/delivery/v1"
TARGETS = "INGOT_DELIVERY_TARGETS"

MANAGED_MCP = "managed-mcp"
FILESYSTEM = "filesystem"
KINDS = (MANAGED_MCP, FILESYSTEM)

VAULT_TARGET = "vault"


@dataclass(frozen=True)
class Target:
    name: str
    kind: str
    root: Path

    def path(self, skill: str) -> Path:
        return self.root / skill


def _parse_one(entry: str, *, vault: Path) -> Target:
    name, separator, remainder = entry.partition("=")
    kind, kind_separator, raw_path = remainder.partition(":")
    if not separator or not kind_separator:
        raise ValueError(f"invalid delivery target {entry!r}: expected name=kind:path")
    name, kind, raw_path = name.strip(), kind.strip(), raw_path.strip()
    if not SLUG_RE.fullmatch(name):
        raise ValueError(f"invalid delivery target name {name!r}")
    if kind not in KINDS:
        raise ValueError(f"unknown delivery kind {kind!r}; expected one of {', '.join(KINDS)}")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        # A relative root resolves against whatever directory the publisher happened to start in,
        # which for a systemd unit is not a directory anyone chose.
        raise ValueError(f"delivery target {name!r} must be an absolute path, not {raw_path!r}")
    return Target(name, kind, path)


def _contained(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def parse_targets(spec: str, *, vault: Path) -> tuple[Target, ...]:
    """Read the configured target list, refusing anything that cannot work.

    Every check here is one the publisher must make before it starts. A delivery target that fails
    on the first approval strands a change that has already been approved, which is the stalled-lane
    failure the backend configuration validation exists to prevent."""
    vault = vault.expanduser()
    targets: list[Target] = []
    seen: dict[str, Target] = {}
    roots: dict[Path, str] = {}
    for entry in (piece.strip() for piece in spec.split(",")):
        if not entry:
            continue
        target = _parse_one(entry, vault=vault)
        if target.name in seen:
            raise ValueError(f"duplicate delivery target {target.name!r}")
        if target.kind == MANAGED_MCP and target.root != vault:
            raise ValueError(f"the managed-mcp target must be the vault ({vault}), "
                             f"not {target.root}")
        if target.kind == FILESYSTEM and _contained(target.root, vault):
            raise ValueError(f"delivery target {target.name!r} is inside the vault; it would write "
                             f"the checkout the publisher just committed")
        if target.root in roots:
            raise ValueError(f"delivery targets {roots[target.root]!r} and {target.name!r} name the "
                             f"same directory")
        seen[target.name] = target
        roots[target.root] = target.name
        targets.append(target)
    # The vault is not optional. It is what the MCP server serves and what publication authority is
    # measured against, so a configuration that omits it gets it anyway rather than silently
    # switching off managed delivery.
    if not any(target.kind == MANAGED_MCP for target in targets):
        if VAULT_TARGET in seen:
            raise ValueError(f"delivery target {VAULT_TARGET!r} is reserved for the managed-mcp "
                             f"vault target")
        targets.insert(0, Target(VAULT_TARGET, MANAGED_MCP, vault))
    return tuple(targets)


def load_targets(env: dict | None = None, *, vault: Path) -> tuple[Target, ...]:
    env = os.environ if env is None else env
    return parse_targets(env.get(TARGETS) or "", vault=vault)


def observed(target: Target, skill: str) -> str:
    """The revision the target actually holds, asked of the filesystem.

    Absence is a revision: a skill a target does not carry is a real, checkable state, and the
    publisher delivers it deliberately when a rollback restores one."""
    path = target.path(skill)
    if not path.is_dir():
        return promote.ABSENT_REVISION
    return skill_revision(path)


def _snapshot_displaced(target: Target, skill: str) -> None:
    """Preserve whatever the target held, under the revision of the bytes actually there.

    Keyed by observation rather than by the receipt's champion: a target someone edited by hand
    holds bytes no release describes, and those are the ones worth keeping."""
    path = target.path(skill)
    if not path.is_dir():
        return
    promote._snapshot(path, skill, skill_revision(path))


def _refuse_symlinks(source: Path) -> None:
    """A delivered tree carries no symlinks, for the same reason an admitted one carries none.

    `copytree` without `symlinks=True` copies what a link points at, so a link to somewhere outside
    the skill would land in a native agent's skill root as an ordinary file holding those bytes.
    Copying the link instead is no better: it puts a path in an agent's library leading out of it.
    Admission already refuses symlinks, so reaching this means the vault acquired one some other
    way, and delivering it is not this code's decision to make."""
    stack = [source]
    while stack:
        for entry in stack.pop().iterdir():
            if entry.is_symlink():
                raise ValueError(
                    f"symlink-unsupported: {entry.relative_to(source)} is a symbolic link")
            if entry.is_dir():
                stack.append(entry)


def _swap(staged: Path, destination: Path) -> None:
    """Replace one directory with another using same-filesystem renames only.

    POSIX has no atomic directory swap, so this is two renames with the displaced directory kept
    until the second one succeeds. The window between them is the only moment the destination is
    absent, and a failure inside it puts the original back rather than leaving a half-installed
    skill an agent could load."""
    if not destination.exists():
        os.replace(staged, destination)
        return
    displaced = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.old")
    os.replace(destination, displaced)
    try:
        os.replace(staged, destination)
    except BaseException:
        os.replace(displaced, destination)
        raise
    shutil.rmtree(displaced, ignore_errors=True)


def _remove(destination: Path) -> None:
    if not destination.exists():
        return
    displaced = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.old")
    os.replace(destination, displaced)
    shutil.rmtree(displaced, ignore_errors=True)


def install(target: Target, skill: str, source: Path | None, revision: str) -> bool:
    """Install one approved revision at one target. True when the target was written.

    `source` is the vault's copy of the skill, already verified to be the approved revision, or
    None to deliver an absence. `revision` is what the receipt names, and it is the staged copy --
    the bytes that will actually be installed -- that is checked against it, so a source altered
    between the vault check and this call cannot install itself."""
    promote.check_slug(skill)
    if target.kind == MANAGED_MCP:
        return False
    destination = target.path(skill)
    if observed(target, skill) == revision:
        return False
    _snapshot_displaced(target, skill)
    if source is None or revision == promote.ABSENT_REVISION:
        _remove(destination)
        return True
    _refuse_symlinks(source)
    target.root.mkdir(parents=True, exist_ok=True)
    staged = target.root / f".{skill}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copytree(source, staged, symlinks=False)
        if skill_revision(staged) != revision:
            raise ValueError(f"delivered '{skill}' does not match the approved revision")
        _swap(staged, destination)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return True
