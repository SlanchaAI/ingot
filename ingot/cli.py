"""The `ingot` command line.

Nothing here writes a served byte. `add` quarantines, `approve` and `rollback` queue a publication
receipt, `reject` discards a quarantined change: the publisher is the only writer of the served
library, and every mutating verb calls the same service the console calls rather than a second
approval path of its own.

Every import stays inside the function that needs it. Importing this module must not pull in
FastAPI, ONNX, LangGraph, Langfuse, or the optimizer, because the first thing a developer runs has
to work in a bare virtualenv with no services, no model, and no key."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

LIST_SCHEMA = "ingot/list/v1"


def _default_actor() -> str:
    """Who a proposal is attributed to. Best effort, and never blank: an unattributed proposal in
    the review queue is one nobody can ask about."""
    return os.environ.get("INGOT_ACTOR") or getpass.getuser()


def list_library(root: Path | None = None) -> dict:
    """The skills a server would serve, with the roots they came from.

    `root` is passed to the loader rather than replacing its configuration: `configured_roots`
    always puts the local authoring root first, even ahead of an explicit root, so the answer can
    legitimately include skills from somewhere the caller did not name. Reporting `roots` is what
    keeps that honest -- a caller who sees an unexpected skill can see which library it came from."""
    from ingot.mcp_server.registry import configured_roots, load_skills

    explicit = [root] if root is not None else None
    return {
        "schema_version": LIST_SCHEMA,
        "roots": [str(path) for path in configured_roots(explicit)],
        "skills": [{"name": skill.name,
                    "description": skill.description,
                    "revision": skill.revision,
                    "root": skill.root}
                   for skill in load_skills(roots=explicit)],
    }


def _render(result: dict) -> str:
    roots = ", ".join(result["roots"])
    skills = result["skills"]
    if not skills:
        return f"No skills in {roots}"
    width = max(len(skill["name"]) for skill in skills)
    lines = [f"{len(skills)} skill{'s' if len(skills) != 1 else ''} in {roots}", ""]
    lines += [f"  {skill['name']:<{width}}  {skill['revision'][:8]}  {skill['description']}"
              for skill in skills]
    return "\n".join(lines)


def _list(args: argparse.Namespace) -> int:
    result = list_library(args.root)
    print(json.dumps(result, indent=2) if args.json else _render(result))
    return 0


def _review(args: argparse.Namespace) -> int:
    """Exit non-zero only for deterministic validity errors. Warnings are advice, and a command
    that fails on advice teaches people to stop reading it."""
    from . import review as review_module

    package = args.path
    if not package.is_dir():
        print(f"ingot review: {package} is not a directory", file=sys.stderr)
        return 2

    result = review_module.review_package(package, library_root=args.root)
    print(json.dumps(result, indent=2) if args.json else review_module.render(result))
    return 0 if result["valid"] else 1


def _add(args: argparse.Namespace) -> int:
    """Quarantine a package. Never activates anything, so the only failures are refusals."""
    from . import admission

    try:
        kind, resolved = admission.parse_locator(args.locator)
        if kind == "file":
            if args.skill:
                raise admission.AdmissionRefused("--skill is only valid for github: sources")
            result = admission.add_package(resolved, actor=args.actor)
        else:
            if not args.skill:
                raise admission.AdmissionRefused("--skill is required for github: sources")
            import tempfile
            from pathlib import Path
            from . import acquire

            with tempfile.TemporaryDirectory() as temporary:
                package, provenance = acquire.github(
                    resolved, ref="HEAD", subdirectory=args.skill,
                    destination=Path(temporary))
                result = admission.add_package(
                    package, actor=args.actor, source_type="github", locator=args.locator,
                    provenance=provenance)
    except (admission.AdmissionRefused, ValueError) as refusal:
        print(f"ingot add: {refusal}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    verb = "already quarantined" if result["status"] == "duplicate" else "quarantined"
    review_hint = (f"  ingot review {result['candidate']['source']['locator']}\n"
                   if result["candidate"]["source"]["type"] == "file" else "")
    print(f"{verb} '{result['skill']}' as proposal {result['proposal_id']}\n"
          f"  revision  {result['candidate']['candidate_revision'][:16]}\n"
          f"  source    {result['candidate']['source']['locator']}\n"
          f"\nThe served library is unchanged. Review and approve it in the console, or:\n"
          f"{review_hint}"
          f"  ingot approve {result['skill']}")
    return 0


def _vault_init(args: argparse.Namespace) -> int:
    from . import vault

    try:
        result = vault.init_vault(args.path)
    except ValueError as refusal:
        print(f"ingot vault init: {refusal}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"{result['status']} vault at {result['path']}\n"
          f"  branch  {result['branch']}\n"
          f"  head    {result['head'][:12]}")
    if result["added"]:
        print(f"  added   {', '.join(result['added'])}")
    return 0


def _status(args: argparse.Namespace) -> int:
    """Exit non-zero when the served library is writable, so a managed deployment can assert it."""
    from . import status as status_module

    result = status_module.library_status(args.root)
    print(json.dumps(result, indent=2) if args.json else status_module.render(result))
    return 0 if result["mode"] == status_module.MANAGED else 1


def _when(seconds: object) -> str:
    """Unix seconds as a local timestamp, or blank. A record written before the field existed must
    print as an empty column rather than a traceback."""
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")


def _pending(args: argparse.Namespace) -> int:
    from . import decisions

    result = decisions.pending_view()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    if result["unreadable"]:
        print(f"WARNING {len(result['unreadable'])} quarantined change(s) cannot be read and are "
              f"not listed: {', '.join(result['unreadable'])}", file=sys.stderr)
    if not result["pending"] and not result["publishing"]:
        print("Nothing waiting.")
        return 0
    for entry in result["pending"]:
        verdict = "ready" if entry["promotable"] else "BLOCKED"
        print(f"  {verdict:<8} {entry['skill']:<20} {entry['kind']:<12} "
              f"{entry['revision'][:12]}")
        for reason in entry["blocked"]:
            print(f"           {reason}")
        if entry["publication"]:
            print(f"           publication {entry['publication']['status']}")
    for entry in result["publishing"]:
        print(f"  {entry['status']:<8} {entry['skill']:<20} {entry['action']:<12} "
              f"{entry['revision'][:12]}")
        if entry["error"]:
            print(f"           {entry['error']}")
    return 0


def _decide(args: argparse.Namespace) -> int:
    """Approve, reject, or roll back. Every one queues or discards; none writes a served byte."""
    from . import decisions

    try:
        if args.command == "approve":
            result = decisions.approve(args.skill, actor=args.actor)
        elif args.command == "reject":
            result = decisions.reject(args.skill, actor=args.actor, reason=args.reason)
        else:
            result = decisions.rollback(args.skill, args.revision, actor=args.actor)
    except ValueError as refusal:
        print(f"ingot {args.command}: {refusal}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(result["result"])
    receipt = result["publication"]
    if receipt:
        print(f"  publication {receipt['id']}  {receipt['status']}")
        if receipt["error"]:
            print(f"  {receipt['error']}")
        if receipt["status"] != "published":
            print("  The served library is unchanged until the publisher activates it.")
    return 0


def _history(args: argparse.Namespace) -> int:
    from . import decisions

    try:
        result = decisions.history_view(args.skill)
    except ValueError as refusal:
        print(f"ingot history: {refusal}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"{result['skill']}\n")
    print("  snapshots (rollback targets)")
    for revision in result["revisions"] or []:
        print(f"    {revision['revision'][:16]}  {_when(revision.get('created'))}")
    if not result["revisions"]:
        print("    none")
    print("\n  publications")
    for receipt in result["publications"] or []:
        print(f"    {receipt['status']:<10} {receipt['action']:<9} "
              f"{(receipt['revision'] or '')[:16]}  {receipt['id']}")
    if not result["publications"]:
        print("    none")
    print("\n  decisions")
    for record in result["audit"] or []:
        print(f"    {record.get('action', ''):<10} {record.get('actor', ''):<16} "
              f"{(record.get('revision') or '')[:16]}  {_when(record.get('ts'))}"
              + (f"  {record['reason']}" if record.get("reason") else ""))
    if not result["audit"]:
        print("    none")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingot",
        description="Release control for agent skills.")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="list the skills in a library")
    listing.add_argument("--root", type=Path, default=None,
                         help="library root to read instead of the configured one")
    listing.add_argument("--json", action="store_true", help="emit the versioned JSON payload")
    listing.set_defaults(handler=_list)

    reviewing = sub.add_parser(
        "review", help="report what is wrong with a skill package, offline",
        description="Deterministic, model-free, network-free, read-only review of one skill "
                    "package. Reports six sections and no composite score; questions it cannot "
                    "answer offline are reported UNMEASURED with the command that answers them.")
    reviewing.add_argument("path", type=Path, help="the skill package directory to review")
    reviewing.add_argument("--root", type=Path, default=None,
                           help="library root to check for collisions against")
    reviewing.add_argument("--json", action="store_true", help="emit the versioned JSON payload")
    reviewing.set_defaults(handler=_review)

    adding = sub.add_parser(
        "add", help="quarantine a skill package for review",
        description="Review a package and place it in quarantine. The served library is left "
                    "byte-identical; a human must approve the proposal before anything is served.")
    adding.add_argument("locator", help="file:./path/to/skill, a bare path, or github:OWNER/REPO")
    adding.add_argument("--skill", help="package subdirectory inside a github: repository")
    adding.add_argument("--actor", default=_default_actor(),
                        help="who is submitting this (defaults to the current user)")
    adding.add_argument("--json", action="store_true", help="emit the versioned JSON payload")
    adding.set_defaults(handler=_add)

    queue = sub.add_parser(
        "pending", help="list quarantined changes waiting on a decision",
        description="Everything waiting on a person, plus anything already travelling to the "
                    "vault. Read-only.")
    queue.add_argument("--json", action="store_true", help="emit the versioned JSON payload")
    queue.set_defaults(handler=_pending)

    approving = sub.add_parser(
        "approve", help="approve a quarantined change for publication",
        description="Queues a publication receipt. It does not activate anything: the publisher "
                    "commits the approved revision and only then does the library serve it.")
    approving.add_argument("skill")

    rejecting = sub.add_parser(
        "reject", help="discard a quarantined change",
        description="Deletes the pending record and records the decision in the approval trail.")
    rejecting.add_argument("skill")
    rejecting.add_argument("--reason", default="", help="why, for the approval trail")

    reverting = sub.add_parser(
        "rollback", help="queue a stored snapshot for publication",
        description="Takes the same lane as an approval: the snapshot is published through the "
                    "publisher rather than copied over the served library.")
    reverting.add_argument("skill")
    reverting.add_argument("revision", help="a revision from `ingot history SKILL`")

    for decision in (approving, rejecting, reverting):
        decision.add_argument("--actor", default=_default_actor(),
                              help="who is deciding (defaults to the current user)")
        decision.add_argument("--json", action="store_true",
                              help="emit the versioned JSON payload")
        decision.set_defaults(handler=_decide)

    past = sub.add_parser(
        "history", help="what a skill has been, and what was decided about it",
        description="Rollback targets, publication receipts, and the approval trail for one "
                    "skill. Read-only.")
    past.add_argument("skill")
    past.add_argument("--json", action="store_true", help="emit the versioned JSON payload")
    past.set_defaults(handler=_history)

    vault = sub.add_parser(
        "vault", help="the Git vault the publisher owns",
        description="The local publication backend publishes into a Git repository on this "
                    "machine. This creates one, and is idempotent against an existing vault.")
    vault_sub = vault.add_subparsers(dest="vault_command", required=True)
    initialize = vault_sub.add_parser("init", help="create or complete a local vault")
    initialize.add_argument("path", type=Path, nargs="?", default=Path("vault"),
                            help="where the vault lives (default: ./vault)")
    initialize.add_argument("--json", action="store_true", help="emit the versioned JSON payload")
    initialize.set_defaults(handler=_vault_init)

    reporting = sub.add_parser(
        "status", help="report whether this deployment's guarantees hold",
        description="MANAGED when the served library is read-only to this process, so only the "
                    "publisher can change what is served. UNMANAGED otherwise, and the exit code "
                    "says so: 0 for MANAGED, 1 for UNMANAGED.")
    reporting.add_argument("--root", type=Path, default=None,
                           help="library root to inspect instead of the configured one")
    reporting.add_argument("--json", action="store_true", help="emit the versioned JSON payload")
    reporting.set_defaults(handler=_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
