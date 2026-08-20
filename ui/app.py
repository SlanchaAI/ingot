"""Change-control UI: the review surface for quarantined instruction changes.

Reviewers see the evidence and the promotion decision first. SkillOpt optimization is a core
workflow that only ever writes to the pending queue. Promotion and
rollback both go through `ingot.optimize.promote`, which snapshots the displaced revision and swaps
directories atomically.
"""
import difflib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from ingot.mcp_server.registry import SLUG_RE, load_skills, read_components, skill_revision
from ingot.optimize import harbor_report, resolve_skill_dir
from ingot.optimize.ab import TASKS_DIR, run_ab
from ingot.optimize.local_traces import LOCAL_TRACE_FILE, store_summary
from ui.auth import (auth_mode, current_actor, require_auth, require_role, using_default_password)
from ingot.optimize.promote import (ABSENT_REVISION, _audit_best_effort, approve_pending, list_pending, list_revisions,
                                    list_snapshotted_skills, load_pending, load_snapshot_components,
                                    pending_path, read_audit, reject_pending, rollback,
                                    stale_evidence_reason)
from ingot.optimize.publication import (publication_for_skill, publishing_skills, recent_publications)
from ingot import paths

logger = logging.getLogger(__name__)
# Recorded evidence locations are written relative to the state root, so this is what they
# resolve against -- not the code's directory, which no longer holds state.
STATE_ROOT = paths.runs().parent


def _evidence_dir() -> Path:
    return (paths.runs() / "evidence").resolve()
# Bundles written inside a container recorded their container-absolute path before evidence
# locations became repo-relative. Both forms name the same file from the host checkout.
CONTAINER_ROOT = Path("/app")


def _check(skill: str) -> str:
    if not SLUG_RE.fullmatch(skill):
        raise HTTPException(400, "invalid skill name")
    return skill


def same_origin(request: Request):
    """CSRF guard on state-changing endpoints: a cross-site page can POST to localhost (a paid
    SkillOpt run, a silent promotion, or a rollback) without being able to read the response. Require
    the request to originate from this app's own origin."""
    origin = request.headers.get("origin")
    if origin is None:  # non-browser client (curl, the demo's own scripts), no ambient cookies to abuse
        return
    if urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "cross-origin request refused")

app = FastAPI(title="ingot change control",
              description="Review evidence for quarantined skill changes, promote them "
                          "atomically, and roll back a promoted revision.",
              # Compose uses a LAN-grade Basic-auth gate; a bare process with no credentials
              # infers open mode. Additional users live in runs/auth.json (see ui/auth.py).
              dependencies=[Depends(require_auth)])

if using_default_password():
    logger.warning("change-control UI is using the DEFAULT password, set AUTH_PASSWORD in .env "
                   "before exposing it beyond your own machine")

# OIDC (Sign in with Google) profile: a signed-session cookie + the /auth/* browser flow. Wired only
# in this mode so password/open deployments carry no session machinery; validate_oidc_config() fails
# closed at startup if the config is incomplete (see ui/auth.py, docs/sso.md).
if auth_mode() == "oidc":
    import os

    from starlette.middleware.sessions import SessionMiddleware

    from ui.auth import oidc_cookie_kwargs, validate_oidc_config
    from ui.oidc_flow import router as oidc_router
    validate_oidc_config()
    app.add_middleware(SessionMiddleware, secret_key=os.environ["SESSION_SECRET"],
                       **oidc_cookie_kwargs())
    app.include_router(oidc_router)
else:
    # Password mode can surface the Basic username in the board. Open mode has no signed-in user.
    # The OIDC router owns the richer session-backed endpoint when that profile is active.
    @app.get("/auth/me")
    def auth_me(actor: str = Depends(current_actor)):
        password = auth_mode() == "password"
        return {"authenticated": password, "email": "", "name": actor if password else "",
                "role": "admin" if password else ""}

RUNS: dict[str, dict] = {}  # skill -> {"status": running|done|error, "action": eval|review|optimize, "log": [lines]}
RUN_LOCK = threading.Lock()


@app.get("/")
def index(request: Request):
    # OIDC mode: bounce an unauthenticated visitor straight to the provider (no interstitial page).
    if auth_mode() == "oidc" and not request.session.get("user"):
        return RedirectResponse("/auth/login")
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/config")
def config():
    return {"langfuse_url": os.environ.get("LANGFUSE_PUBLIC_URL", "http://localhost:3100")}


@app.get("/api/traces")
def traces(project: str = "", harness: str = "", skill: str = "", since: str = "",
           until: str = "", include_tasks: bool = False):
    """Safe console projection of locally normalized coding-agent turns.

    Answers stay in the on-disk store used by the explicit mining command. The browser receives
    task text and attribution metadata, never answer text, reasoning, or tool payloads.
    """
    try:
        return store_summary(LOCAL_TRACE_FILE, project=project, harness=harness, skill=skill,
                             since=since, until=until, include_tasks=include_tasks)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.get("/api/harbor")
def harbor_matrices():
    """Skills that have a cross-harness matrix on disk."""
    return {"skills": harbor_report.available()}


@app.get("/api/harbor/{skill}")
def harbor_matrix(skill: str):
    """The harness x model matrix for one skill: does this skill help this combination, and by how
    much, judged in a sandbox against the same held-out tasks with and without the skill.

    404 when the skill has never been run, so "not measured yet" cannot be read as "no effect".
    """
    _check(skill)
    try:
        matrix = harbor_report.read_matrix(skill)
    except ValueError as error:
        raise HTTPException(503, str(error)) from error
    if matrix is None:
        raise HTTPException(404, f"no cross-harness run for '{skill}'; run "
                                 f"`python -m ingot.optimize.harbor_eval {skill} --agent <harness>`")
    return matrix


@app.get("/api/clusters")
def clusters():
    """The category buckets, as last computed. Clustering loads the embedding model, which is too
    heavy for a request on the poll path, so it is a command that writes the file and this only
    serves it. 404 names the command rather than implying the feature is missing."""
    from ingot.optimize.cluster import CLUSTER_PATH
    if not CLUSTER_PATH.exists():
        raise HTTPException(404, "no clusters computed yet; run "
                                 "`docker compose run --rm --entrypoint python optimize "
                                 "-m ingot.optimize.cluster`")
    try:
        data = json.loads(CLUSTER_PATH.read_text())
    except (OSError, ValueError) as e:  # a half-written or hand-edited file is not a 500
        raise HTTPException(503, f"clusters file is unreadable ({e}); re-run ingot.optimize.cluster")
    indexed = {s.name for s in _cached_load_skills()}
    # Skills come and go between clustering runs. Say so rather than drawing a stale map as fact.
    for cluster in data.get("clusters", []):
        for member in cluster.get("members", []):
            member["missing"] = member["name"] not in indexed
    data["stale_members"] = sum(m.get("missing", False)
                                for c in data.get("clusters", []) for m in c.get("members", []))
    data["unclustered"] = len(indexed) - sum(len(c.get("members", []))
                                             for c in data.get("clusters", []))
    return data


_SKILLS_CACHE = None
_SKILLS_CACHE_KEY = None


def _get_skills_cache_key():
    """Fast cache key based on mtime of skill directories and SKILL.md files."""
    from ingot.mcp_server.registry import configured_roots, skill_sources
    roots = configured_roots()
    mtimes = [id(load_skills)]
    for r in roots:
        if r.exists():
            mtimes.append((str(r), r.stat().st_mtime))
            for src in skill_sources(r):
                mtimes.append((str(src), src.stat().st_mtime))
                skill_dir = src.parent
                mtimes.append((str(skill_dir), skill_dir.stat().st_mtime))
    return tuple(mtimes)


def _cached_load_skills():
    global _SKILLS_CACHE, _SKILLS_CACHE_KEY
    current_key = _get_skills_cache_key()
    if _SKILLS_CACHE is not None and current_key == _SKILLS_CACHE_KEY:
        return _SKILLS_CACHE
    skills_list = load_skills()
    _SKILLS_CACHE = skills_list
    _SKILLS_CACHE_KEY = current_key
    return skills_list


@app.get("/api/skills")
def skills():
    tasksets = {p.stem for p in TASKS_DIR.glob("*.yaml")}
    from ingot.mcp_server.usage_counts import load_counts
    from ingot.mcp_server import provenance
    counts = load_counts()
    # One parsed ledger per root, not per skill: a merged library re-reads the same VENDORED.md
    # once for every skill it serves otherwise.
    ledgers: dict = {}
    # Approval does not free the review slot: the pending record is held until the vault commit
    # lands, so an approved change keeps reading as one still awaiting a decision.
    publishing = publishing_skills()
    active = [
        {"name": s.name, "description": s.description, "has_tasks": s.name in tasksets,
         "pending": load_pending(s.name) is not None, "revision": s.revision,
         "publishing": s.name in publishing,
         "uses": counts.get(s.name, 0),
         "provenance": provenance.classify(s.name, s.root, ledgers=ledgers),
         "status": RUNS.get(s.name, {}).get("status")}
        for s in _cached_load_skills()
        if SLUG_RE.fullmatch(s.name)  # a non-slug name (hostile frontmatter) can't be optimized anyway
    ]
    active_names = {item["name"] for item in active}
    creations = []
    for pending in list_pending():
        if pending.get("kind") != "creation" or pending.get("skill") in active_names:
            continue
        components = pending.get("challenger_components") or {}
        creations.append({"name": pending["skill"],
                          "description": str(components.get("description", "")),
                          "has_tasks": False, "pending": True, "revision": "", "uses": 0,
                          "publishing": pending["skill"] in publishing,
                          "provenance": "proposed", "status": None, "active": False})
    return active + creations


def _active_skill(skill: str):
    match = next((item for item in _cached_load_skills() if item.name == skill), None)
    if match is None:
        raise HTTPException(404, f"no active skill named '{skill}'")
    return match


def _pending_revision(skill, pending: dict) -> str:
    recorded = _challenger_revision(pending)
    if recorded:
        return recorded
    try:
        return skill_revision(Path(skill.root), pending["challenger_components"])
    except (KeyError, OSError, TypeError, ValueError):
        return ""


def _version_payload(skill: str, kind: str, revision: str, created: int | None,
                     components: dict[str, str]) -> dict:
    files = [{"path": key[len("file:"):], "content": value}
             for key, value in sorted(components.items()) if key.startswith("file:")]
    return {"skill": skill, "kind": kind, "revision": revision, "created": created,
            "description": components.get("description", ""),
            "body": components.get("body", ""), "files": files}


@app.get("/api/skills/{skill}/versions")
def skill_versions(skill: str):
    """The live, pending, and snapshotted versions available to inspect for one active skill."""
    active = _active_skill(_check(skill))
    versions = [{"key": "active", "kind": "active", "revision": active.revision,
                 "created": None}]
    pending = load_pending(skill)
    if pending and isinstance(pending.get("challenger_components"), dict):
        versions.append({"key": "pending", "kind": "pending",
                         "revision": _pending_revision(active, pending),
                         "created": pending.get("created")})
    versions.extend({"key": item["revision"], "kind": "snapshot", **item}
                    for item in list_revisions(skill) if item["revision"] != ABSENT_REVISION)
    return {"skill": skill, "description": active.description, "versions": versions}


@app.get("/api/skills/{skill}/versions/{version}")
def skill_version(skill: str, version: str):
    """Read one version's instructions and bundled text files without changing active state."""
    active = _active_skill(_check(skill))
    if version == "active":
        return _version_payload(skill, "active", active.revision, None,
                                read_components(Path(active.root)))
    if version == "pending":
        pending = load_pending(skill)
        components = pending.get("challenger_components") if pending else None
        if not isinstance(components, dict):
            raise HTTPException(404, f"no pending version for '{skill}'")
        return _version_payload(skill, "pending", _pending_revision(active, pending),
                                pending.get("created"), components)
    try:
        components = load_snapshot_components(skill, version)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    created = next((item["created"] for item in list_revisions(skill)
                    if item["revision"] == version), None)
    return _version_payload(skill, "snapshot", version, created, components)


@app.post("/api/optimize/{skill}",
          dependencies=[Depends(same_origin), Depends(require_role("proposer"))])
def optimize(skill: str):
    """Start a SkillOpt optimization for one skill. It never activates anything: the
    result is a quarantined pending record for review."""
    _check(skill)
    _preflight_optimize(skill)
    state, log = _start_run(skill, "optimize")

    threading.Thread(target=_run_optimization, args=(skill, state, log), daemon=True).start()
    return {"started": skill}


def _preflight_optimize(skill: str) -> None:
    _preflight_provider()
    if not (TASKS_DIR / f"{skill}.yaml").exists():
        raise HTTPException(404, f"no eval task set for '{skill}'")


def _start_run(skill: str, action: str) -> tuple[dict, Callable[..., None]]:
    # Drafting and optimization share a process-global token ledger and OpenRouter budget. Claim
    # the slot under a lock: FastAPI runs sync handlers concurrently, so a check before assignment
    # lets two paid requests both pass.
    with RUN_LOCK:
        if any(s.get("status") == "running" for s in RUNS.values()):
            raise HTTPException(409, "a paid model run is already in progress")
        state = RUNS[skill] = {"status": "running", "action": action, "log": []}

    def log(*args):
        state["log"].append(" ".join(str(a) for a in args))
        if len(state["log"]) > 1000:
            state["log"] = state["log"][-1000:]

    return state, log


@app.post("/api/evals/{skill}",
          dependencies=[Depends(same_origin), Depends(require_role("proposer"))])
def create_eval_set(skill: str):
    """Draft the missing train/holdout task set without starting an optimization."""
    _active_skill(_check(skill))
    _preflight_provider()
    if (TASKS_DIR / f"{skill}.yaml").exists():
        raise HTTPException(409, f"'{skill}' already has an eval task set")
    state, log = _start_run(skill, "eval")
    threading.Thread(target=_run_eval_draft, args=(skill, state, log), daemon=True).start()
    return {"started": skill}


def _eval_payload(skill: str) -> dict:
    path = TASKS_DIR / f"{skill}.yaml"
    if not path.exists():
        raise HTTPException(404, f"no eval task set for '{skill}'")
    try:
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError("top level must be a mapping")
        train = data.get("train") or data.get("tasks") or []
        explicit_holdout = bool(data.get("holdout"))
        holdout = data.get("holdout") or train
        routing = data.get("routing") or []
        acceptance = data.get("acceptance") or []
        if not all(isinstance(section, list)
                   for section in (train, holdout, routing, acceptance)):
            raise ValueError("train, holdout, routing, and acceptance must be lists")
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise HTTPException(
            503, f"eval set is unreadable for '{skill}' ({e}); repair its task file") from e
    tasks = list(train) + (list(holdout) if explicit_holdout else [])
    return {
        "skill": skill, "train": train, "holdout": holdout, "routing": routing,
        "acceptance": acceptance, "leakage": not explicit_holdout,
        "counts": {
            "train": len(train), "holdout": len(holdout), "routing": len(routing),
            "acceptance": len(acceptance),
            "checks": sum(len(task.get("checklist") or [])
                          for task in tasks if isinstance(task, dict)),
        },
    }


@app.get("/api/evals/{skill}")
def eval_set(skill: str):
    """The exact task-set inputs the optimizer reads, exposed so its scores stay traceable."""
    _active_skill(_check(skill))
    return _eval_payload(skill)


@app.get("/api/reviews/{skill}")
def review_result(skill: str):
    """The newest persisted current-skill review. Reviews measure; they never propose or activate."""
    active = _active_skill(_check(skill))
    from ingot.optimize.review import REVIEW_DIR
    path = REVIEW_DIR / f"{skill}.json"
    if not path.exists():
        raise HTTPException(404, f"no review result for '{skill}'")
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("top level must be an object")
        data.setdefault("created", int(path.stat().st_mtime))
        recorded = data.get("revision")
        if not isinstance(recorded, str) or not recorded:
            data["stale"] = "review did not record a skill revision; run it again"
        elif recorded != active.revision:
            data["stale"] = "active skill changed since this review ran; run it again"
        else:
            data["stale"] = None
        return data
    except (OSError, ValueError) as e:
        raise HTTPException(503, f"review result is unreadable ({e}); run the review again") from e


@app.post("/api/reviews/{skill}",
          dependencies=[Depends(same_origin), Depends(require_role("proposer"))])
def start_review(skill: str):
    """Score the active skill against its evals without creating a pending revision."""
    _active_skill(_check(skill))
    _preflight_provider()
    _eval_payload(skill)  # refuse missing or malformed measurement inputs before spending
    state, log = _start_run(skill, "review")
    threading.Thread(target=_run_review, args=(skill, state, log), daemon=True).start()
    return {"started": skill}


def _preflight_provider() -> None:
    from ingot.optimize import openrouter_key_missing, preflight_provider_pins
    if openrouter_key_missing():
        raise HTTPException(400, "API_KEY is not set, copy .env.example to .env, "
                                 "add your key (https://openrouter.ai/keys), and restart the stack "
                                 "(or point BASE_URL/MODEL_BASE_URL at a local endpoint)")
    try:
        preflight_provider_pins()
    except SystemExit as e:  # pin/model conflict, surface the explanation, don't start a run
        raise HTTPException(400, str(e))


def _run_optimization(skill: str, state: dict, log) -> None:
    try:
        run_ab(skill, log=log)
        state["status"] = "done"
    except BaseException as e:  # surface SystemExit etc. in the UI
        log(f"ERROR: {e}")
        state["status"] = "error"


def _run_eval_draft(skill: str, state: dict, log) -> None:
    try:
        from ingot.optimize import usage as usage_ledger
        from ingot.optimize.draft import draft_and_save

        usage_ledger.reset()
        components = read_components(resolve_skill_dir(skill))
        # The teacher may fail after opening its output. Stage beside the mounted task directory,
        # then hard-link the complete file into place so failure cannot create a bogus eval set
        # and a concurrent writer cannot be overwritten.
        with tempfile.TemporaryDirectory(prefix=f".draft-{skill}-", dir=TASKS_DIR) as staging:
            staged = draft_and_save(skill, components["description"], components["body"],
                                    Path(staging), log=log)
            try:
                os.link(Path(staged), TASKS_DIR / f"{skill}.yaml")
            except FileExistsError:
                raise RuntimeError(f"'{skill}' already has an eval task set") from None
        state["status"] = "done"
    except BaseException as e:  # surface provider, parse, and SystemExit failures in the card
        log(f"ERROR: {e}")
        state["status"] = "error"


def _run_review(skill: str, state: dict, log) -> None:
    try:
        from ingot.optimize.review import run_review
        run_review(skill, log=log)
        state["status"] = "done"
    except BaseException as e:  # provider, judge, and spend-cap failures belong in the skill log
        log(f"ERROR: {e}")
        state["status"] = "error"


@app.get("/api/runs")
def runs():
    return {skill: {"status": s["status"], "action": s.get("action", "optimize"),
                    "log": s["log"][-30:]} for skill, s in RUNS.items()}


_COMPONENT_LABEL = {"description": "SKILL.md (description)", "body": "SKILL.md (body)",
                    "frontmatter": "SKILL.md (frontmatter)"}


def _label(component: str) -> str:
    return _COMPONENT_LABEL.get(component, component[len("file:"):] if component.startswith("file:") else component)


def _inner_loop(record: dict) -> dict | None:
    """Candidate-search scores recorded by current SkillOpt optimization runs."""
    return record.get("inner_loop")


def _review_risk(champion: dict, challenger: dict) -> dict:
    """Line and body-size risk metrics shown before a reviewer opens the full diff."""
    before = str(champion.get("body", "")).splitlines()
    after = str(challenger.get("body", "")).splitlines()
    removed = added = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after).get_opcodes():
        if tag != "equal":
            removed += i2 - i1
            added += j2 - j1
    changed_pct = round(100 * max(added, removed) / max(len(before), len(after), 1), 1)
    before_chars = len(str(champion.get("body", "")))
    after_chars = len(str(challenger.get("body", "")))
    size_delta_pct = round(100 * (after_chars - before_chars) / max(before_chars, 1), 1)
    return {"added_lines": added, "removed_lines": removed, "changed_pct": changed_pct,
            "size_delta_pct": size_delta_pct,
            "high_risk": changed_pct >= 50 or size_delta_pct <= -50}


def _publication_matches_pending(publication: dict, pending: dict) -> bool:
    """Whether a skill-level receipt belongs to this exact quarantined proposal."""
    proposal_id = next((str((pending.get(kind) or {}).get("proposal_id") or "")
                        for kind in ("retrospective", "creation")
                        if (pending.get(kind) or {}).get("proposal_id")), "")
    revision = str(((pending.get("evidence") or {}).get("challenger") or {}).get("revision") or "")
    identities = []
    if proposal_id:
        identities.append(str(publication.get("proposal_id") or "") == proposal_id)
    if revision:
        identities.append(str(publication.get("candidate_revision") or "") == revision)
    if not identities:
        identities.append(publication.get("components") == pending.get("challenger_components"))
    return all(identities)


@app.get("/api/pending/{skill}")
def pending(skill: str):
    p = load_pending(_check(skill))
    if not p:
        raise HTTPException(404, f"no pending change for '{skill}'")
    champ, chall = p["champion_components"], p["challenger_components"]
    blocks = []
    changed_components = (p.get("changed_components") or
                          [key for key in champ if champ[key] != chall.get(key, "")])
    for comp in changed_components:
        label = _label(comp)
        blocks.append("\n".join(difflib.unified_diff(
            str(champ.get(comp, "")).splitlines(), str(chall.get(comp, "")).splitlines(),
            fromfile=f"{label} (champion)", tofile=f"{label} (challenger)", lineterm="")))
    comparison = [{"component": _label(component), "before": str(champ.get(component, "")),
                   "after": str(chall.get(component, ""))}
                  for component in changed_components]
    publication = publication_for_skill(skill)
    if publication and not _publication_matches_pending(publication, p):
        publication = None
    # `pr` and `note` are what make a stalled publication legible: a receipt sitting at
    # awaiting_merge because the vault does not allow auto-merge is waiting on a person, and the
    # reviewer has no other way to learn which pull request to go and merge.
    publication_view = ({key: publication.get(key) for key in
                         ("id", "state", "actor", "action", "attempts", "last_error", "pr", "note")}
                        if publication else None)
    return {"skill": skill, "kind": p.get("kind", "quality"), "inner_loop": _inner_loop(p),
            "ab": p.get("ab"), "routing": p.get("routing"), "dataset": p.get("dataset"),
            "retrospective": p.get("retrospective"), "creation": p.get("creation"),
            "publication": publication_view,
            "evidence": p.get("evidence_paths"), "stale": _stale_reason(skill, p),
            "model": p.get("model"), "judge": p.get("judge"),
            "gate": p.get("gate", {"promotable": True, "blocked": []}),
            "risk": _review_risk(champ, chall), "comparison": comparison,
            "changed": [_label(c) for c in changed_components],
            "diff": "\n\n".join(blocks)}


def _stale_reason(skill: str, pending: dict) -> str | None:
    """Freshness is re-checked when the card is rendered, not only when Approve is clicked, so a
    change whose champion moved on disk is refused before a reviewer commits to it. A failure to
    answer is not a verdict: the approval path re-checks and is the authority."""
    try:
        return stale_evidence_reason(skill, pending)
    except (OSError, KeyError, TypeError, ValueError):
        logger.warning("Could not re-check evidence freshness for %r", skill, exc_info=True)
        return None


CHANGE_LOCK = threading.Lock()


@contextmanager
def change_control(skill: str):
    """Serialize the two request paths that write under `skills/`, and refuse the second rather
    than queue it.

    A promotion and a rollback each snapshot, stage, and swap directories over several steps, and
    the stores they touch assume one logical writer (see ARCHITECTURE, Stores and ownership). The
    endpoints run on a thread pool, so two clicks, or a click and a scripted POST, can interleave
    those steps and swap a directory the other has already renamed. One lock covers every skill:
    a local operator never has two of these in flight, and a queued second action would apply to
    a revision the reviewer never saw. This mirrors the one-at-a-time candidate-run guard."""
    if not CHANGE_LOCK.acquire(blocking=False):
        raise HTTPException(409, f"another approval or rollback is already in progress; "
                                 f"retry the action for '{skill}' when it finishes")
    try:
        yield
    finally:
        CHANGE_LOCK.release()


@app.post("/api/promote/{skill}",
          dependencies=[Depends(same_origin), Depends(require_role("approver"))])
def approve(skill: str, actor: str = Depends(current_actor)):
    p = load_pending(_check(skill))
    if not p:
        raise HTTPException(404, f"no pending change for '{skill}'")
    if p.get("gate", {}).get("promotable") is not True:
        raise HTTPException(409, "the evidence gate blocked this change")
    with change_control(skill):
        try:
            return {"result": approve_pending(skill, actor=actor)}
        except ValueError as e:  # stale evidence: the champion moved on disk since the run
            raise HTTPException(409, str(e))


PUBLICATION_FIELDS = ("id", "skill", "action", "state", "actor", "attempts",
                      "pr", "note", "last_error", "created")
LIVE_STATES = ("approved_publishing", "publishing", "awaiting_merge")


def _pending_blocked() -> str | None:
    """A human-readable warning when a pending record exists but cannot be used, else None."""
    from ingot.optimize.promote import pending_dir, unreadable_pending
    queue = pending_dir()
    if queue.is_dir() and not os.access(queue, os.R_OK | os.X_OK):
        return (f"cannot read the review queue at {queue} as uid {os.getuid()}; "
                f"quarantined changes cannot be listed")
    blocked = unreadable_pending()
    if not blocked:
        return None
    return (f"{len(blocked)} quarantined change(s) in {queue} cannot be read as uid "
            f"{os.getuid()} and are NOT shown below: {', '.join(blocked)}")


@app.get("/api/publications")
def publications():
    """The publication lane, read only.

    A receipt outlives the pending record it came from, so this is the only surface that can show
    an approved change while it is still travelling to the vault. `unreadable` is not cosmetic:
    `Path.glob` swallows `PermissionError`, so a store this process cannot list looks exactly like
    an empty one, and an empty lane is the reading a stalled publisher most wants you to make."""
    # Read the receipt store through the module attribute, not a name bound at import: the path is
    # configurable, `recent_publications` looks it up per call, and a frozen copy here inspected one
    # directory while listing another.
    from ingot.optimize.publication import publications_dir

    # Only the forge backend has a pull request to link to, and only it knows the repository.
    forge = os.environ.get("INGOT_FORGE_REPOSITORY") or ""
    store = publications_dir()
    unreadable = store.is_dir() and not os.access(store, os.R_OK | os.X_OK)
    records = [] if unreadable else recent_publications()
    return {"publications": [dict({key: record.get(key) for key in PUBLICATION_FIELDS},
                                  pr_url=(f"https://github.com/{forge}/pull/{record['pr']}"
                                          if forge and record.get("pr") else None))
                             for record in records],
            "live_states": list(LIVE_STATES),
            "unreadable": (f"cannot read the receipt store at {store} as uid "
                           f"{os.getuid()}; publications cannot be listed") if unreadable else None,
            # The same misreading, one directory over. `list_pending` skips a record it cannot read
            # so one corrupt file cannot break review, which also means an unreadable proposal is
            # indistinguishable from no proposal and the board reports CLEAR over it. Observed live:
            # the MCP container writes records as root 0600 while this process is uid 1000.
            "pending_blocked": _pending_blocked()}


@app.get("/api/evidence/{skill}")
def evidence(skill: str):
    """The recorded evidence bundle for a pending change, read only.

    Only the path the pending record itself wrote is opened, and only when it resolves inside
    runs/evidence. Nothing a request carries selects a file."""
    path = _evidence_file(_recorded_location(_check(skill)))
    markdown = _read_evidence(path)
    return {"skill": skill, "path": path.relative_to(_evidence_dir()).as_posix(),
            "markdown": markdown}


def _recorded_location(skill: str) -> str:
    """The evidence location the pending record wrote for itself, or a 404 naming what is missing.
    The path comes from the record, never from the request."""
    p = load_pending(skill)
    if not p:
        raise HTTPException(404, f"no pending change for '{skill}'")
    recorded = (p.get("evidence_paths") or {}).get("markdown")
    if isinstance(recorded, str) and recorded.strip():
        return recorded
    raise HTTPException(404, f"no evidence bundle recorded for '{skill}'")


def _read_evidence(path: Path) -> str:
    """A bundle a record still points at can be gone or unreadable; that is a missing bundle for
    the reviewer, not a server fault."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise HTTPException(404, "the recorded evidence bundle is no longer readable")


def _host_path(recorded: str) -> Path:
    """A recorded location as a path in this checkout: repo-relative, or the host equivalent of a
    container-absolute one. Any other absolute path is left alone for the containment check to
    refuse."""
    path = Path(recorded)
    if not path.is_absolute():
        return STATE_ROOT / path
    try:
        return STATE_ROOT / path.relative_to(CONTAINER_ROOT)
    except ValueError:
        return path


def _evidence_file(recorded: str) -> Path:
    """Resolve a recorded evidence location to a file inside runs/evidence, or refuse it.
    Resolution happens before the containment check, so neither `..` nor a symlink out of the
    evidence tree can reach another part of the filesystem."""
    resolved = _host_path(recorded).resolve()
    if not resolved.is_relative_to(_evidence_dir()):
        raise HTTPException(400, "recorded evidence path is outside runs/evidence")
    return resolved


def _challenger_revision(pending: dict) -> str:
    """The challenger's revision hash from a pending record's evidence, or '' when none is recorded
    (e.g. a creation-kind pending). Mirrors the revision approve/rollback audit."""
    revision = ((pending.get("evidence") or {}).get("challenger") or {}).get("revision")
    return revision if isinstance(revision, str) else ""


class RejectRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


@app.post("/api/reject/{skill}",
          dependencies=[Depends(same_origin), Depends(require_role("approver"))])
def reject(skill: str, payload: RejectRequest | None = None,
           actor: str = Depends(current_actor)):
    _check(skill)
    # Load, validate, delete, and audit all under the lock: checking existence first and deleting
    # later would let a second reject pass the check, then re-delete and double-audit after the
    # first released, returning 200 instead of 404 (mirrors approve/rollback holding the lock).
    with change_control(skill):
        try:
            result = reject_pending(skill, actor=actor,
                                    reason=payload.reason if payload else "")
        except ValueError as exc:
            raise HTTPException(404, str(exc))
    return {"result": result}


@app.get("/api/history")
def history():
    """Rollback targets plus the metadata-only approval trail, newest first.

    Snapshot names come from the snapshot store rather than a second pass over the skill library:
    the skills listing already hashes every skill on each refresh, and doing it twice per poll is
    the whole cost of this view. Each half degrades on its own, so one unreadable store does not
    blank the other."""
    return {"revisions": _rollback_targets(), "audit": _audit_page()}


def _snapshotted_skills() -> list[str]:
    """Naming the snapshot store is its own failure: an unreadable `runs/revisions/` raises before
    any per-skill listing is attempted, and the approval trail is still readable."""
    try:
        return list_snapshotted_skills()
    except (OSError, ValueError):
        logger.warning("Could not read the snapshot store", exc_info=True)
        return []


def _rollback_targets() -> dict[str, list[dict]]:
    targets = {}
    for name in _snapshotted_skills():
        try:
            revisions = list_revisions(name)
        except (OSError, ValueError):
            logger.warning("Could not list snapshots for %r", name, exc_info=True)
            continue
        if revisions:
            targets[name] = revisions
    return targets


def _audit_page() -> dict:
    try:
        return read_audit()
    except (OSError, ValueError):
        logger.warning("Could not read the approval trail", exc_info=True)
        return {"records": [], "total": 0}


@app.post("/api/rollback/{skill}/{revision}",
          dependencies=[Depends(same_origin), Depends(require_role("approver"))])
def rollback_revision(skill: str, revision: str, actor: str = Depends(current_actor)):
    with change_control(_check(skill)):
        try:
            return {"result": rollback(skill, revision, actor=actor)}
        except ValueError as e:
            raise HTTPException(404, str(e))
