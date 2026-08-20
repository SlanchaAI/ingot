"""Whether this deployment's guarantees actually hold, reported as an observation.

The verdict is not read back out of configuration. It compares what is served against what the
last successful release receipt says should be served, per skill, and reports the worst answer.
A configuration flag would have agreed with the claim rather than checked it, which is the failure
this command exists to catch."""
from __future__ import annotations

import os
from pathlib import Path

STATUS_SCHEMA = "ingot/status/v1"

MANAGED = "MANAGED"        # served bytes are the last successful release
PENDING = "PENDING"        # a proposal or publication is in flight
DRIFTED = "DRIFTED"        # served bytes differ from the last successful release
UNMANAGED = "UNMANAGED"    # no release receipt covers these bytes, or development mode

# Worst first. Drift is the alarm; an unmanaged skill is one the guarantee never covered; a
# publication in flight is expected and transient.
SEVERITY = (DRIFTED, UNMANAGED, PENDING, MANAGED)
ABSENT = "absent"


def _writable(path: Path) -> bool:
    """Whether this process could change what is served.

    `os.access` rather than a permission-bit reading: it accounts for the read-only mount managed
    mode relies on, which no mode bit describes. Reported as a fact rather than folded into the
    verdict — the administrator who owns the vault can always write it, and a status command that
    answered UNMANAGED from their shell would hide the drift they most need to see."""
    return path.is_dir() and os.access(path, os.W_OK | os.X_OK)


def _worst(states) -> str:
    for state in SEVERITY:
        if state in states:
            return state
    return MANAGED


def skill_states(root: Path | None = None) -> list[dict]:
    """One verdict per skill, plus every skill a release receipt names but nothing serves."""
    from ingot.mcp_server.registry import load_skills
    from ingot.optimize.promote import list_pending
    from ingot.optimize.publication import latest_releases, publishing_skills

    explicit = [root] if root is not None else None
    served = {skill.name: skill.revision for skill in load_skills(roots=explicit)}
    releases = latest_releases()
    in_flight = publishing_skills() | {record.get("skill") for record in list_pending()}

    states = []
    # In-flight names join the union: a creation that is quarantined or travelling is served by
    # nothing and released by nothing, so a status built from those two sets alone would report an
    # empty library as fully MANAGED while a publication was in progress.
    for name in sorted(set(served) | set(releases) | {name for name in in_flight if name}):
        current = served.get(name, ABSENT)
        release = releases.get(name)
        released = release.get("candidate_revision") if release else None
        if released is None:
            # Nothing Ingot published is responsible for these bytes: they were fetched, copied,
            # or committed to the vault by hand. Real, common, and not drift — there is no release
            # to have drifted from.
            state = PENDING if name in in_flight else UNMANAGED
        elif current != released:
            state = DRIFTED
        elif name in in_flight:
            state = PENDING
        else:
            state = MANAGED
        states.append({"skill": name, "state": state, "revision": current,
                       "released": released,
                       "publication": release.get("id") if release else None})
    return states


def target_states(env: dict | None = None) -> list[dict]:
    """One verdict per delivery target, decided the same way as the library's: by looking.

    A target is graded only on the skills Ingot released there or has in flight for it. A native
    skill root is shared with whatever its owner put in it, and those skills are not Ingot's to
    judge -- grading them would report every real deployment as permanently UNMANAGED and bury the
    one line that matters. A released skill that has been deleted from a target is still drift:
    absence is a revision, and it is not the released one."""
    from ingot import delivery
    from ingot.optimize.promote import list_pending
    from ingot.optimize.publication import latest_releases, publishing_skills

    from . import paths

    env = os.environ if env is None else env
    targets = delivery.load_targets(env, vault=paths.vault(env))
    releases = latest_releases()
    in_flight = {name for name in
                 publishing_skills() | {record.get("skill") for record in list_pending()} if name}

    reported = []
    for target in targets:
        skills = []
        for name in sorted(set(releases) | in_flight):
            released = (releases.get(name) or {}).get("candidate_revision")
            current = delivery.observed(target, name)
            if released is None:
                state = PENDING if name in in_flight else UNMANAGED
            elif current != released:
                state = DRIFTED
            elif name in in_flight:
                state = PENDING
            else:
                state = MANAGED
            skills.append({"skill": name, "state": state, "revision": current,
                           "released": released})
        reported.append({"name": target.name, "kind": target.kind, "root": str(target.root),
                         "state": _worst({entry["state"] for entry in skills}), "skills": skills})
    return reported


def library_status(root: Path | None = None, env: dict | None = None) -> dict:
    from ingot.mcp_server.registry import configured_roots

    from . import paths

    env = os.environ if env is None else env
    # An explicit root means exactly that root. `configured_roots` always prepends the local
    # authoring library even ahead of one, which is right for serving and wrong here: asked whether
    # a particular library is managed, this must not answer about a different one.
    roots = ([Path(root).expanduser().resolve()] if root is not None
             else [Path(path) for path in configured_roots()])
    development = (env.get("INGOT_MODE") or "").strip().lower() in {"dev", "unmanaged"}
    states = skill_states(root)
    # A misconfigured target list must not take the command down. Status is what an operator runs
    # when something is already wrong, so a broken variable is a finding to report, not a crash.
    try:
        targets, delivery_error = target_states(env), None
    except (ValueError, OSError) as exc:
        targets, delivery_error = [], str(exc)
    verdicts = {entry["state"] for entry in states}
    verdicts |= {entry["state"] for entry in targets}
    if delivery_error:
        verdicts.add(DRIFTED)
    mode = UNMANAGED if development else _worst(verdicts)
    return {
        "schema_version": STATUS_SCHEMA,
        "mode": mode,
        "development_mode": development,
        "uid": os.getuid(),
        "roots": [str(path) for path in roots],
        "writable_roots": [str(path) for path in roots if _writable(path)],
        "skills": states,
        "targets": targets,
        "delivery_error": delivery_error,
        "publish_backend": env.get("INGOT_PUBLISH_BACKEND") or "local",
        "vault_path": str(paths.vault(env)),
        "forge_repository": env.get("INGOT_FORGE_REPOSITORY"),
        "paths": paths.resolved(env),
        # Never migrated, only reported: moving a review queue or a receipt store on someone's
        # behalf is a change to controlled state made by a process nobody asked to make it.
        "legacy_state": paths.legacy_state(),
    }


_EXPLANATION = {
    MANAGED: "Every served skill is exactly the revision its last release receipt names.",
    PENDING: "A proposal or publication is in flight. Nothing has drifted.",
    DRIFTED: "Served bytes differ from the last successful release. Something changed them "
             "outside the publisher.",
    UNMANAGED: "Some served bytes have no release receipt behind them, so the quarantine and "
               "publication guarantees do not describe them.",
}


def render(result: dict) -> str:
    lines = [f"{result['mode']}  (uid {result['uid']})", ""]
    if result["development_mode"]:
        lines.append("  Development mode (INGOT_MODE). The served library is writable by every")
        lines.append("  service and control-plane guarantees do not apply to this deployment.")
    else:
        lines.append(f"  {_EXPLANATION[result['mode']]}")
    for entry in result["skills"]:
        if entry["state"] == MANAGED:
            continue
        detail = (f"served {entry['revision'][:12]} != released {entry['released'][:12]}"
                  if entry["state"] == DRIFTED else
                  "in flight" if entry["state"] == PENDING else "no release receipt")
        lines.append(f"    {entry['state']:<10} {entry['skill']:<20} {detail}")
    if result["writable_roots"] and not result["development_mode"]:
        lines += ["", "  Writable by this process, so nothing here stops this user from changing",
                  "  what is served without an approval:"]
        lines += [f"    {path}" for path in result["writable_roots"]]
    if result.get("delivery_error"):
        lines += ["", "  The delivery target configuration cannot be read, so nothing here knows",
                  f"  what this deployment installs or where: {result['delivery_error']}"]
    for target in result.get("targets") or []:
        drifted = [entry for entry in target["skills"] if entry["state"] != MANAGED]
        if target["state"] == MANAGED and not drifted:
            continue
        lines += ["", f"  {target['state']:<10} target {target['name']} ({target['kind']}) "
                      f"{target['root']}"]
        for entry in drifted:
            detail = (f"holds {entry['revision'][:12]} != released {entry['released'][:12]}"
                      if entry["state"] == DRIFTED else
                      "in flight" if entry["state"] == PENDING else "no release receipt")
            lines.append(f"    {entry['state']:<10} {entry['skill']:<20} {detail}")
    lines += ["", f"  backend  {result['publish_backend']}"]
    if result["forge_repository"]:
        lines.append(f"  forge    {result['forge_repository']}")
    lines.append(f"  roots    {', '.join(result['roots'])}")
    for target in result.get("targets") or []:
        lines.append(f"  deliver  {target['name']:<12} {target['kind']:<12} {target['root']}  "
                     f"{target['state']}")
    lines += ["", "  state"]
    width = max(len(entry["name"]) for entry in result["paths"])
    for entry in result["paths"]:
        note = "" if entry["writable"] else "  NOT WRITABLE"
        lines.append(f"    {entry['name']:<{width}}  {entry['path']}  [{entry['source']}]{note}")
    if result["legacy_state"]:
        lines += ["", "  State left beside the code by an earlier version. Nothing here reads it;",
                  "  move what you want to keep under the paths above, then delete it:"]
        lines += [f"    {path}" for path in result["legacy_state"]]
    return "\n".join(lines)
