"""Change-control UI: the review surface for quarantined instruction changes.

Reviewers see the evidence and the promotion decision first; candidate generation (the optional
generator) is a secondary action that only ever writes to the pending queue. Promotion and
rollback both go through `optimize.promote`, which snapshots the displaced revision and swaps
directories atomically.
"""
import difflib
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from mcp_server.registry import SLUG_RE, load_skills
from optimize.ab import TASKS_DIR, run_ab
from ui.auth import (auth_mode, current_actor, require_auth, require_role, using_default_password)
from optimize.promote import (approve_pending, list_pending, list_revisions,
                              list_snapshotted_skills, load_pending, pending_path, read_audit,
                              rollback, stale_evidence_reason)

logger = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = (REPO_ROOT / "runs" / "evidence").resolve()
# Bundles written inside a container recorded their container-absolute path before evidence
# locations became repo-relative. Both forms name the same file from the host checkout.
CONTAINER_ROOT = Path("/app")


def _check(skill: str) -> str:
    if not SLUG_RE.fullmatch(skill):
        raise HTTPException(400, "invalid skill name")
    return skill


def same_origin(request: Request):
    """CSRF guard on state-changing endpoints: a cross-site page can POST to localhost (a paid
    candidate run, a silent promotion, a rollback) without being able to read the response. Require
    the request to originate from this app's own origin."""
    origin = request.headers.get("origin")
    if origin is None:  # non-browser client (curl, the demo's own scripts), no ambient cookies to abuse
        return
    if urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "cross-origin request refused")

app = FastAPI(title="ingot change control",
              description="Review evidence for quarantined skill changes, promote them "
                          "atomically, and roll back a promoted revision.",
              # LAN-grade gate: no-op when no users file exists (local default stays open);
              # HTTP Basic against runs/auth.json once a user is added (see ui/auth.py).
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

RUNS: dict[str, dict] = {}  # skill -> {"status": running|done|error, "log": [lines]}


@app.get("/")
def index(request: Request):
    # OIDC mode: bounce an unauthenticated visitor straight to the provider (no interstitial page).
    if auth_mode() == "oidc" and not request.session.get("user"):
        return RedirectResponse("/auth/login")
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/config")
def config():
    return {"langfuse_url": os.environ.get("LANGFUSE_PUBLIC_URL", "http://localhost:3100")}


@app.get("/api/skills")
def skills():
    active = _active_skill_rows({p.stem for p in TASKS_DIR.glob("*.yaml")})
    return active + _pending_creation_rows(active)


def _active_skill_rows(tasksets: set[str]) -> list[dict]:
    from mcp_server.usage_counts import load_counts
    counts = load_counts()
    return [
        {"name": s.name, "description": s.description, "has_tasks": s.name in tasksets,
         "pending": load_pending(s.name) is not None, "revision": s.revision,
         "uses": counts.get(s.name, 0),
         "status": RUNS.get(s.name, {}).get("status"), "creation": False}
        for s in load_skills()
        if SLUG_RE.fullmatch(s.name)  # a non-slug name (hostile frontmatter) can't be optimized anyway
    ]


def _pending_creation_row(record: dict, active_names: set[str]) -> dict | None:
    if record.get("kind") != "creation":
        return None
    name = record["skill"]
    if name in active_names:
        return None
    description = record.get("challenger_components", {}).get("description")
    if not isinstance(description, str):
        return None
    return {
        "name": name,
        "description": description,
        "has_tasks": False,
        "pending": True,
        "revision": None,
        "status": None,
        "creation": True,
    }


def _pending_creation_rows(active: list[dict]) -> list[dict]:
    active_names = {item["name"] for item in active}
    creations = []
    for record in list_pending():
        creation = _pending_creation_row(record, active_names)
        if creation:
            creations.append(creation)
    return creations


@app.post("/api/optimize/{skill}",
          dependencies=[Depends(same_origin), Depends(require_role("proposer"))])
def optimize(skill: str):
    """Start the optional candidate generator for one skill. It never activates anything: the
    result is a quarantined pending record for review."""
    _check(skill)
    _preflight_optimize(skill)
    state = RUNS[skill] = {"status": "running", "log": []}

    def log(*args):
        state["log"].append(" ".join(str(a) for a in args))

    threading.Thread(target=_run_optimization, args=(skill, state, log), daemon=True).start()
    return {"started": skill}


def _preflight_optimize(skill: str) -> None:
    _preflight_provider()
    if not (TASKS_DIR / f"{skill}.yaml").exists():
        raise HTTPException(404, f"no eval task set for '{skill}'")
    # one candidate run at a time: the token ledger is process-global, and concurrent runs
    # would also contend for the same OpenRouter budget
    if any(s.get("status") == "running" for s in RUNS.values()):
        raise HTTPException(409, "a candidate run is already in progress")


def _preflight_provider() -> None:
    from optimize import openrouter_key_missing, preflight_provider_pins
    if openrouter_key_missing():
        raise HTTPException(400, "OPENROUTER_API_KEY is not set, copy .env.example to .env, "
                                 "add your key (https://openrouter.ai/keys), and restart the stack "
                                 "(or point MODEL_BASE_URL/OPENROUTER_BASE_URL at a local endpoint)")
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


@app.get("/api/runs")
def runs():
    return {skill: {"status": s["status"], "log": s["log"][-30:]} for skill, s in RUNS.items()}


_COMPONENT_LABEL = {"description": "SKILL.md (description)", "body": "SKILL.md (body)"}


def _label(component: str) -> str:
    return _COMPONENT_LABEL.get(component, component[len("file:"):] if component.startswith("file:") else component)


def _inner_loop(record: dict) -> dict | None:
    """Candidate-search scores. Records written before the GEPA body loop was removed store these
    under `gepa`; read either so an existing review slot still renders."""
    return record.get("inner_loop") or record.get("gepa")


@app.get("/api/pending/{skill}")
def pending(skill: str):
    p = load_pending(_check(skill))
    if not p:
        raise HTTPException(404, f"no pending change for '{skill}'")
    champ, chall = p["champion_components"], p["challenger_components"]
    blocks = []
    for comp in p.get("changed_components") or [k for k in champ if champ[k] != chall.get(k, "")]:
        label = _label(comp)
        blocks.append("\n".join(difflib.unified_diff(
            champ[comp].splitlines(), chall.get(comp, "").splitlines(),
            fromfile=f"{label} (champion)", tofile=f"{label} (challenger)", lineterm="")))
    return {"skill": skill, "kind": p.get("kind", "quality"), "inner_loop": _inner_loop(p),
            "ab": p.get("ab"), "routing": p.get("routing"), "dataset": p.get("dataset"),
            "evidence": p.get("evidence_paths"), "stale": _stale_reason(skill, p),
            "model": p.get("model"), "judge": p.get("judge"),
            "gate": p.get("gate", {"promotable": True, "blocked": []}),
            "changed": [_label(c) for c in p.get("changed_components", [])], "diff": "\n\n".join(blocks)}


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
        except ValueError as e:  # stale evidence, a name collision, a failed safety re-check
            raise HTTPException(409, str(e))


@app.get("/api/evidence/{skill}")
def evidence(skill: str):
    """The recorded evidence bundle for a pending change, read only.

    Only the path the pending record itself wrote is opened, and only when it resolves inside
    runs/evidence. Nothing a request carries selects a file."""
    path = _evidence_file(_recorded_location(_check(skill)))
    markdown = _read_evidence(path)
    return {"skill": skill, "path": path.relative_to(EVIDENCE_DIR).as_posix(), "markdown": markdown}


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
        return REPO_ROOT / path
    try:
        return REPO_ROOT / path.relative_to(CONTAINER_ROOT)
    except ValueError:
        return path


def _evidence_file(recorded: str) -> Path:
    """Resolve a recorded evidence location to a file inside runs/evidence, or refuse it.
    Resolution happens before the containment check, so neither `..` nor a symlink out of the
    evidence tree can reach another part of the filesystem."""
    resolved = _host_path(recorded).resolve()
    if not resolved.is_relative_to(EVIDENCE_DIR):
        raise HTTPException(400, "recorded evidence path is outside runs/evidence")
    return resolved


@app.post("/api/reject/{skill}",
          dependencies=[Depends(same_origin), Depends(require_role("approver"))])
def reject(skill: str):
    pending_path(_check(skill)).unlink(missing_ok=True)
    return {"result": f"rejected the pending change for '{skill}'"}


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
