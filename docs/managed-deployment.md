# Managed deployment

The claim is that only an approved change reaches what is served. This page is how that is
enforced, how to check it on your own machine, and exactly what it does and does not protect
against.

## One writer

The served skill library is a Git repository — the vault. Every service mounts it read-only except
the publisher, which is the only process allowed to change it and only acts on an approved receipt.

```text
ingot add file:./pkg      quarantine        the library is byte-identical
ingot add github:OWNER/REPO --skill path/to/pkg
                          quarantine        the library is byte-identical
approve                   receipt written   the library is byte-identical
publisher                 commit, activate  the library now serves the approved revision
```

The whole loop from a terminal, none of which writes a served byte:

```bash
ingot add file:./pkg        # quarantine a package for review
ingot add github:OWNER/REPO --skill path/to/pkg
ingot review ./pkg          # what is wrong with it, offline
ingot pending               # what is waiting on a decision
ingot approve csv-tidy      # queue a publication receipt
ingot history csv-tidy      # snapshots, receipts, and the decision trail
ingot rollback csv-tidy <revision>
ingot status                # is this deployment still what was approved?
```

`approve`, `reject`, and `rollback` call the same services the console calls. There is no second
approval path and no command that activates a skill directly.

Approval is a human gate that writes a receipt. It does not touch the library. The publisher reads
the receipt, materializes exactly the components the receipt names, runs the vault's validator,
commits, snapshots the revision it is about to displace, fast-forwards the served checkout, and
re-verifies that what is served is the revision the receipt named. Any mismatch at any step fails
the receipt and changes nothing.

`docker compose up` with no `-f` is the managed stack. That is deliberate: a default that launched
a writable stack while this page described controlled activation would make the claim untrue for
almost every reader.

## Is it actually managed here?

```bash
ingot status
ingot status --json
```

Four answers, decided per skill by comparing what is served against what the last successful
release receipt says should be served:

| | Meaning |
|---|---|
| `MANAGED` | Every served skill is exactly the revision its release receipt names. |
| `PENDING` | A proposal or publication is in flight. Nothing has drifted. |
| `DRIFTED` | Served bytes differ from the last successful release. Something changed them outside the publisher. |
| `UNMANAGED` | Some served bytes have no release receipt behind them, or the deployment is in development mode. |

The deployment reports the worst of them, and exits non-zero for anything but `MANAGED`, so a check
can assert it. This is an observation, not a configuration flag: a flag would have agreed with the
claim rather than tested it, which is the failure this command exists to catch.

A skill with no release receipt is `UNMANAGED`, not `DRIFTED`. Fetched, copied, and hand-committed
skills are real and common; there is no release for them to have drifted from, and calling that
drift would make the alarm mean nothing.

Whether the library is writable by the calling process is reported alongside the verdict but does
not decide it. The administrator who owns the vault can always write it, and a status command that
answered `UNMANAGED` from their shell would hide the drift they most need to see.

## Drift, and what to do about it

A read-only mount does not stop the machine owner from editing the host directory. Rather than
claim it does, Ingot detects it:

```text
DRIFTED  (uid 1000)

  Served bytes differ from the last successful release. Something changed them outside the publisher.
    DRIFTED    csv-tidy    served 35e6c7a05313 != released e660639d81da
```

Two ways back to `MANAGED`:

- **Restore the released bytes.** In the local backend the served checkout is the vault, so the
  edit is an uncommitted change: `git -C vault checkout -- <skill>`. This is an explicit
  administrator action on the vault, not something Ingot does behind the publisher's back.
- **Keep the change and get it approved.** Copy the edited directory somewhere else, restore the
  vault, and submit the copy: `ingot add file:./that-copy`. It goes through review and approval
  like any other proposal.

Note the ordering. Until the vault checkout is clean the publisher refuses to run at all — a dirty
vault is exactly the state it must not build on — so a drifted deployment cannot publish its way
out. That is a deliberate refusal, not a deadlock: restoring first is one command.

**Deliberately not built:** a single `ingot reconcile` that quarantines the drifted bytes for you.
It needs an answer to a question this design has not settled — a reconcile proposal's champion is
the last release while the disk holds the drifted bytes, so the existing freshness check refuses
it, and every way past that either fabricates evidence or opens a second approval path. The two
steps above do the same work with no new mutation path.

## Development mode

```bash
docker compose -f docker-compose.yml -f compose.dev.yaml up
```

Every service gets the library read-write again and the publisher is switched off. It is convenient
for working on Ingot itself. **Quarantine and publication guarantees do not apply to a stack
started this way**, and it must not be the configuration used to substantiate the control-plane
claim. The stack runs `ingot status` at startup so the reason is in the log.

## Where state lives

Nothing mutable is kept beside the code. The served library, the review queue, publication
receipts, evidence bundles, snapshots, and eval task sets all resolve through one setting:

```text
INGOT_HOME                       # $XDG_STATE_HOME/ingot, else ~/.local/state/ingot
├── library/                     # INGOT_LIBRARY   (SKILLS_DIR is the deprecated name)
├── runs/                        # INGOT_RUNS      pending, publications, evidence, revisions
├── tasks/                       # INGOT_TASKS
└── vault/                       # INGOT_VAULT_PATH, defaults to the library
```

`INGOT_HOME` moves all of them; the specific settings override it one at a time, which is what the
compose stack does — every service names the paths it mounted rather than relying on a default.

```bash
ingot status        # prints every resolved path, where it came from, and whether it is writable
```

This is not cosmetic. A `pip install ingot` used to keep its review queue and its receipts inside
`site-packages`, which meant an upgrade discarded them, a read-only or system Python could not
start, and two deployments sharing one installation shared one queue. If `ingot status` finds state
left there by an earlier version it says so and does nothing else: moving a review queue on your
behalf is a change to controlled state made by a process nobody asked to make it.

## Publication backends

Selected explicitly with `INGOT_PUBLISH_BACKEND`, never inferred. A vault that later gains a remote
does not start opening pull requests on its own.

### `local` (default)

The vault is a Git repository on this machine. No network, no GitHub account, no `gh`, no remote
origin required. `ingot vault init <path>` creates one; the managed compose runs it on every start
and it is idempotent.

States: `approved_publishing → publishing → active`. There is no `awaiting_merge`, because there is
nothing external to wait for — the human gate is the approval.

The vault may have other legitimate writers; a person committing to it directly is fine and the
publisher fast-forwards onto their work. What the publisher will never do is rebase, merge, or
force. If a publication branch can no longer fast-forward, the receipt fails with an inspectable
error and nothing moves; the next attempt re-cuts the branch from the vault as it now stands and
re-checks the champion, so an unrelated commit resolves itself and a conflicting one is refused.

### `forge` (opt-in)

```bash
INGOT_FORGE_REPOSITORY=owner/repo \
  docker compose -f docker-compose.yml -f compose.forge.yaml up
```

Publication authority becomes a merged pull request. States:
`approved_publishing → publishing → awaiting_merge → active`. The vault must already be a clone of
the configured repository. The publisher verifies `gh` is present, authenticated, and that the
repository resolves — at startup, loudly, rather than on the first approval.

This anchors activation somewhere a local administrator cannot quietly rewrite. It also ends the
air gap.

| | `local` | `forge` |
|---|---|---|
| Network | none | required |
| Authority | the approval | a merged pull request |
| Activation record | local Git history | Git history, mirrored off-box |
| Air-gappable | yes | no |

## Delivery targets

The vault is the managed-MCP library: agents that load skills through Ingot's MCP server read the
same checkout the publisher commits into. An agent that reads a native skill directory on disk
reads nothing at all. A delivery target is that second destination.

Configure them with `INGOT_DELIVERY_TARGETS`, a comma-separated list of `name=kind:path`:

```sh
INGOT_DELIVERY_TARGETS=claude=filesystem:~/.claude/skills,codex=filesystem:~/.codex/skills
```

Two kinds:

| Kind | What it is |
|---|---|
| `managed-mcp` | the vault itself, always present, always named `vault` unless you name it |
| `filesystem` | a directory the publisher installs approved revisions into |

Ingot knows nothing about Codex or Claude beyond those names being yours to choose. A filesystem
target is a directory; what reads it is not Ingot's business.

**What delivery does not change.** Publication stays receipt-driven and human-approved, and the
publisher stays the only supported writer. Delivery runs *after* the vault serves the approved
revision and *before* the receipt is marked `active`, so a target that cannot be written leaves a
release that retries rather than one that reports itself finished in places it never reached. Each
target's outcome is recorded on the receipt separately, under `delivery`.

**The managed target is a deliberate no-op.** It has a name, a status, and a line on every receipt,
but the publication commit and the fast-forward are the only things that write the vault. A second
writer there is the one thing this control plane exists to prevent.

**Installing is atomic.** The approved revision is staged beside the destination and swapped in with
same-filesystem renames. A failure between the two renames puts the displaced directory back, so an
agent never loads a skill folder that is neither the old revision nor the new one. Whatever the
target held is snapshotted first — keyed by the revision of the bytes actually there, so a target
someone edited by hand is recoverable too.

**Rollback needs nothing extra.** It travels the ordinary publication queue, so every target returns
to the prior approved revision on the way through.

**Drift is per target.** `ingot status` reports each one separately, and a drifted target counts
toward the overall verdict — a status that answered MANAGED while a native skill root served the
wrong bytes would be the lie the command exists to prevent. A target is graded only on the skills
Ingot released there: a native skill root is shared with whatever its owner put in it, and those are
not Ingot's to judge.

`route_and_load` is unchanged and stays the managed-MCP delivery contract. A native agent activates
from its own skill directory in whatever way it already does; Ingot's router is not mandatory.

## Recovery

`process()` is re-entrant, and a kill at any point leaves a state the next pass resolves:

| Killed | On restart |
|---|---|
| before the worktree is cut | re-prepared from scratch |
| worktree cut, before the commit | the stale worktree is destroyed and recut |
| after the commit, before activation | the branch is reused, re-authorized, activated |
| after activation, before the receipt | the receipt is finalized; **nothing is re-snapshotted** |
| after the receipt | no work; the stored state is returned |

The fourth row is the one that matters. The served bytes already equal the candidate, so the
champion a second snapshot would capture is gone; re-snapshotting would refuse a publication that
has in fact already activated.

## Artifact fidelity

A revision names the exact package. `ingot add` stages every regular file byte-for-byte into a
**candidate tree** under `runs/candidates/<digest>/`, and the receipt carries a manifest recording
each file's relative path, mode, size, and SHA-256 of its raw bytes. Publication copies that staged
tree into the vault worktree and verifies every hash on the way, so a file whose bytes moved between
the approval and the publication stops the publication instead of being served.

Hashes are of bytes, never of decoded text: a file that is not valid UTF-8 has no decoded form, and
one that is would hash differently after a round trip — which is exactly how files used to go
missing.

Two behaviours are deliberate, and both are visible rather than silent:

- **SKILL.md is normalized, not preserved.** Its frontmatter is the routing interface, so the name
  is forced to the skill's identity, the description is collapsed to one line, and the file is
  re-emitted through a safe YAML dump. The manifest still records the source file's real hash and
  size, so the normalization is auditable. The approved revision is computed by performing exactly
  this materialization, so it is the revision the library serves.
- **Symlinks are refused.** `ingot review` reports `symlink-unsupported` and `ingot add` stops.
  Preserving a link puts a path into the vault that leads a reader back out of the library;
  flattening it into its target silently changes the artifact's shape. Neither is a decision
  admission should make on an operator's behalf.

For `github:`, acquisition resolves the public repository's `HEAD` to a commit before cloning and
refuses if the cloned commit differs. It inspects the selected Git tree before fetching its blobs,
rejects gitlinks and symlinks, then reads each blob without a checkout. Repository attributes and
checkout filters cannot rewrite or execute while those bytes enter quarantine. The candidate
records the repository, requested ref, commit, subdirectory, and tree digest.

Assets a reviewer cannot read are reported rather than refused:

```console
$ ingot review ./csv-tidy
structural
  warning binary-asset: 1 file(s) are not text and cannot be read before approval; they will be
          published byte-for-byte: assets/logo.png
```

Decodability decides, not the file extension — an extension is a claim about a file, and the point
is to check the file. Editor and VCS metadata (`.git/`, `__pycache__/`, `.DS_Store`) is not skill
content and is not reported; a finding that fires on `.DS_Store` is one people learn to scroll past.

Modes are clamped to `0644` or `0755`, the two a Git checkout reproduces. A package is capped at
256 files and 20 MB; both refusals name the limit.

Staged trees are named by their digest, so resubmitting the same package reuses one directory
rather than making a second copy. Nothing removes them afterwards: a rejected proposal leaves its
tree in `runs/candidates/`, bounded by the per-package cap, and deleting them is a housekeeping
decision rather than something publication should make on its own.

## What the audit trail actually guarantees

Publication history is Git-backed, revision-bound, and externally anchorable in `forge` mode.
Stated plainly, because a control plane that overstates this is worse than one that has none:

- **Revision-bound.** Every revision is a digest of the exact package — the parsed SKILL.md plus the
  raw bytes of every other file — so the receipt names specific bytes and a moved tag cannot stand
  in for them.
- **Git-backed.** Each publication is a commit with the receipt id in its message, so what was
  served when is reconstructable from history.
- **Detects normal inconsistency.** A champion that changed under a publication, a materialization
  that does not match the approved revision, a staged candidate file whose bytes moved since
  approval, a served checkout that does not match after activation, and a pending review that no
  longer matches its receipt are all refused.
- **Externally anchorable** in `forge` mode, where the activation record exists somewhere the local
  machine does not control.

Git is not by itself proof that history did not change. A local repository can be rewritten by
anyone with a shell in it; a GitHub repository can be force-pushed or administratively altered, and
`forge` mode is an external anchor rather than an immutable transparency log. Someone with root can
rewrite the vault history, the receipts, and the audit log together. A signed log or a real
transparency log is the answer to that threat, and it waits for a concrete threat model rather than
being guessed at now. The records deliberately carry no signature field: a local record an
administrator can rewrite must not carry anything shaped like proof that they did not.

## Proving it on your own machine

```bash
scripts/managed_smoke.sh
```

Starts the managed stack and checks what Docker actually enforces: `mcp` and `ui` must fail to
write the served library, the publisher must succeed on the vault, what `mcp` serves must be the
commit the publisher's vault is at, and `ingot status` inside the stack must report `MANAGED`.

`tests/test_compose_managed.py` checks the same invariant against the tracked YAML on every test
run, which catches a regression in the configuration but cannot prove the containers behave.

CI runs `managed_smoke.sh` on a Linux runner as a required check, and then runs it again with one
`:ro` deliberately removed and requires it to fail. A check that cannot fail proves nothing.

## Running the publisher on the host instead

`ops/systemd/ingot-publisher.service` runs it as a user unit. That sidesteps the uid mismatch
between a container writing receipts at mode 0700 and a host process reading them, and in `forge`
mode it reuses the host's already authenticated `git` and `gh` so no credential has to live in a
container. Copy `ops/systemd/publisher.env.example` to `~/.config/ingot/publisher.env` first.

Whichever you run — the compose service or the unit — exactly one must.
