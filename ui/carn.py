"""Read-only carn panels: replay-graph + trajectory-trie evidence next to the approval UI.

Everything here is gated on CARN_DIR (path to a local carn checkout). Unset -> every
endpoint 404s and the index link stays hidden, so builds without carn are unaffected.
No endpoint mutates anything: this is a viewer over carn's on-disk artifacts.
"""
import importlib.util
import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()

_TF = None  # cached trie_forks module, loaded from CARN_DIR


def carn_enabled() -> bool:
    d = os.environ.get("CARN_DIR")
    return bool(d) and (Path(d) / "scripts" / "trie_forks.py").is_file()


def _carn() -> Path:
    if not carn_enabled():
        raise HTTPException(404, "carn panels disabled (set CARN_DIR to a carn checkout)")
    return Path(os.environ["CARN_DIR"])


def _tf():
    """Import carn's trie_forks.py by path — carn stays the single source of the mining logic."""
    global _TF
    if _TF is None:
        spec = importlib.util.spec_from_file_location(
            "carn_trie_forks", _carn() / "scripts" / "trie_forks.py")
        _TF = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_TF)
    return _TF


def _read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


@router.get("/carn")
def page():
    _carn()
    return FileResponse(Path(__file__).parent / "static" / "carn.html")


@router.get("/api/carn/overview")
def overview():
    d = _carn() / "demo_replay"
    return {"carn_dir": str(_carn()),
            "replay": _read_json(d / "result.json"),
            "rescue": _read_json(d / "rescue_result.json")}


@router.get("/api/carn/graphs")
def graphs():
    d = _carn() / "demo_replay"
    out = []
    for p in sorted(d.glob("*_graph.json")):
        g = _read_json(p)
        if g and isinstance(g.get("nodes"), list):
            out.append({"file": p.name, **g})
    return out


def _list_runs() -> list[dict]:
    tf, root = _tf(), _carn() / "runs"
    if not root.is_dir():
        return []
    out = []
    for traj in sorted(root.glob("**/trajectory.json")):
        d = traj.parent
        if len(d.relative_to(root).parts) > 3:
            continue
        try:
            passed = tf.label_from_tests(str(d))
        except (OSError, ValueError):
            passed = None
        steps = _read_json(traj) or {}
        out.append({"name": str(d.relative_to(root)), "passed": passed,
                    "n_steps": len(steps.get("steps", []))})
    return out


@router.get("/api/carn/runs")
def runs_endpoint():
    return _list_runs()


def _ser(n):
    return {"npass": n.npass, "nfail": n.nfail, "example": n.example, "ends": n.ends,
            "children": {t: _ser(c) for t, c in n.children.items()}}


# the fix-git exemplar from trie_forks.py --self-test: shown when no run dirs exist yet,
# so the panel demonstrates the mechanics instead of rendering an empty page
_EXEMPLAR = [
    ("fix-git-baseline", False, ["cd /app/repo", "git reflog", "git merge a82b384",
     "cat > _includes/about.md << 'ENDOFFILE'\nAbout us...", "git add -A && git commit --no-edit"]),
    ("fix-git-rescue", True, ["cd /app/repo", "git reflog", "git merge a82b384",
     "git checkout a82b384 -- _includes/about.md", "git commit --amend --no-edit",
     "git diff a82b384 -- _includes/about.md"]),
]


@router.get("/api/carn/trie")
def trie(runs: str = ""):
    tf, root = _tf(), _carn() / "runs"
    names = [r for r in runs.split(",") if r] or [r["name"] for r in _list_runs()]
    loaded, demo = [], False
    for name in names:
        d = (root / name).resolve()
        if not d.is_relative_to(root.resolve()):
            raise HTTPException(400, f"run outside runs/: {name}")
        loaded.append(tf.load_run(str(d)))
    if not loaded:
        demo = True
        loaded = [(n, p, tf.tokenize([{"command": c} for c in cmds]))
                  for n, p, cmds in _EXEMPLAR]
    root_node, total, nodes = tf.build(loaded)
    return {"demo": demo,
            "runs": [{"name": n, "passed": p, "tokens": [{"tok": t, "raw": r} for t, r in ts]}
                     for n, p, ts in loaded],
            "trie": _ser(root_node),
            "stats": {"total_steps": total, "nodes": nodes, "shared": total - nodes},
            "forks": [{"path": p} for p, _ in tf.forks(root_node)]}
