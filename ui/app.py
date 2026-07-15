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
from optimize.promote import load_pending, pending_path, promote


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

app = FastAPI(title="skill-router approval UI")

RUNS: dict[str, dict] = {}  # skill -> {"status": running|done|error, "log": [lines]}


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/config")
def config():
    return {"langfuse_url": os.environ.get("LANGFUSE_PUBLIC_URL", "http://localhost:3100")}


@app.get("/api/skills")
def skills():
    tasksets = {p.stem for p in TASKS_DIR.glob("*.yaml")}
    return [
        {"name": s.name, "description": s.description, "has_tasks": s.name in tasksets,
         "pending": load_pending(s.name) is not None,
         "status": RUNS.get(s.name, {}).get("status")}
        for s in load_skills()
        if SLUG_RE.fullmatch(s.name)  # a non-slug name (hostile frontmatter) can't be optimized anyway
    ]


@app.post("/api/optimize/{skill}", dependencies=[Depends(same_origin)])
def optimize(skill: str):
    _check(skill)
    if not (TASKS_DIR / f"{skill}.yaml").exists():
        raise HTTPException(404, f"no eval task set for '{skill}'")
    # one optimization at a time: the token ledger is process-global, and concurrent runs
    # would also contend for the same OpenRouter budget
    if any(s.get("status") == "running" for s in RUNS.values()):
        raise HTTPException(409, "an optimization is already running")
    state = RUNS[skill] = {"status": "running", "log": []}

    def log(*args):
        state["log"].append(" ".join(str(a) for a in args))

    def work():
        try:
            run_ab(skill, log=log)
            state["status"] = "done"
        except BaseException as e:  # surface SystemExit etc. in the UI
            log(f"ERROR: {e}")
            state["status"] = "error"

    threading.Thread(target=work, daemon=True).start()
    return {"started": skill}


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
    return {"skill": skill, "gepa": p["gepa"], "ab": p["ab"], "dataset": p["dataset"],
            "gate": p.get("gate", {"promotable": True, "blocked": []}),
            "changed": [_label(c) for c in p.get("changed_components", [])], "diff": "\n\n".join(blocks)}


@app.post("/api/promote/{skill}", dependencies=[Depends(same_origin)])
def approve(skill: str):
    p = load_pending(_check(skill))
    if not p:
        raise HTTPException(404, f"no pending challenger for '{skill}'")
    if p.get("gate", {}).get("promotable") is not True:
        raise HTTPException(409, "Behavioral CI gate blocked this challenger")
    return {"result": promote(skill)}


@app.post("/api/reject/{skill}", dependencies=[Depends(same_origin)])
def reject(skill: str):
    pending_path(_check(skill)).unlink(missing_ok=True)
    return {"result": f"rejected challenger for '{skill}'"}
