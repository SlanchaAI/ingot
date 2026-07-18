"""Approval UI for champion/challenger evidence and explicit, revisioned promotion."""
import difflib
import os
import threading
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from mcp_server.registry import SLUG_RE, load_skills
from optimize.ab import TASKS_DIR, run_ab
from optimize.promote import approve_pending, list_pending, load_pending, pending_path
from ui.carn import carn_enabled, router as carn_router


def _check(skill: str) -> str:
    if not SLUG_RE.fullmatch(skill):
        raise HTTPException(400, "invalid skill name")
    return skill


def same_origin(request: Request):
    """CSRF guard on state-changing endpoints: a cross-site page can POST to localhost (a paid
    optimize run or a silent promotion) without being able to read the response. Require the
    request to originate from this app's own origin."""
    origin = request.headers.get("origin")
    if origin is None:  # non-browser client (curl, the demo's own scripts) — no ambient cookies to abuse
        return
    if urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "cross-origin request refused")

app = FastAPI(title="ingot approval UI")
app.include_router(carn_router)  # read-only carn panels; inert unless CARN_DIR is set

RUNS: dict[str, dict] = {}  # skill -> {"status": running|done|error, "log": [lines]}


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/config")
def config():
    return {"langfuse_url": os.environ.get("LANGFUSE_PUBLIC_URL", "http://localhost:3100"),
            "carn_enabled": carn_enabled()}


@app.get("/api/skills")
def skills():
    active = _active_skill_rows({p.stem for p in TASKS_DIR.glob("*.yaml")})
    return active + _pending_creation_rows(active)


def _active_skill_rows(tasksets: set[str]) -> list[dict]:
    return [
        {"name": s.name, "description": s.description, "has_tasks": s.name in tasksets,
         "pending": load_pending(s.name) is not None,
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


@app.post("/api/optimize/{skill}", dependencies=[Depends(same_origin)])
def optimize(skill: str):
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
    # one optimization at a time: the token ledger is process-global, and concurrent runs
    # would also contend for the same OpenRouter budget
    if any(s.get("status") == "running" for s in RUNS.values()):
        raise HTTPException(409, "an optimization is already running")


def _preflight_provider() -> None:
    from optimize import openrouter_key_missing, preflight_provider_pins
    if openrouter_key_missing():
        raise HTTPException(400, "OPENROUTER_API_KEY is not set — copy .env.example to .env, "
                                 "add your key (https://openrouter.ai/keys), and restart the stack "
                                 "(or point MODEL_BASE_URL/OPENROUTER_BASE_URL at a local endpoint)")
    try:
        preflight_provider_pins()
    except SystemExit as e:  # pin/model conflict — surface the explanation, don't start a run
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


@app.get("/api/pending/{skill}")
def pending(skill: str):
    p = load_pending(_check(skill))
    if not p:
        raise HTTPException(404, f"no pending challenger for '{skill}'")
    champ, chall = p["champion_components"], p["challenger_components"]
    blocks = []
    for comp in p.get("changed_components") or [k for k in champ if champ[k] != chall.get(k, "")]:
        label = _label(comp)
        blocks.append("\n".join(difflib.unified_diff(
            champ[comp].splitlines(), chall.get(comp, "").splitlines(),
            fromfile=f"{label} (champion)", tofile=f"{label} (challenger)", lineterm="")))
    return {"skill": skill, "kind": p.get("kind", "quality"), "gepa": p.get("gepa"),
            "ab": p.get("ab"), "routing": p.get("routing"), "dataset": p.get("dataset"),
            "gate": p.get("gate", {"promotable": True, "blocked": []}),
            "changed": [_label(c) for c in p.get("changed_components", [])], "diff": "\n\n".join(blocks)}


@app.post("/api/promote/{skill}", dependencies=[Depends(same_origin)])
def approve(skill: str):
    p = load_pending(_check(skill))
    if not p:
        raise HTTPException(404, f"no pending challenger for '{skill}'")
    if p.get("gate", {}).get("promotable") is not True:
        raise HTTPException(409, "Behavioral CI gate blocked this challenger")
    return {"result": approve_pending(skill)}


@app.post("/api/reject/{skill}", dependencies=[Depends(same_origin)])
def reject(skill: str):
    pending_path(_check(skill)).unlink(missing_ok=True)
    return {"result": f"rejected challenger for '{skill}'"}
