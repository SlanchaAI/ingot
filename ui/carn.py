"""Read-only carn panels: replay-graph + trajectory-trie evidence next to the approval UI.

Everything here is gated on CARN_DIR (path to a local carn checkout). Unset -> every
endpoint 404s and the index link stays hidden, so builds without carn are unaffected.
No endpoint mutates anything: this is a viewer over carn's on-disk artifacts.

Trust boundary: _tf() executes $CARN_DIR/scripts/trie_forks.py in-process, so CARN_DIR
is operator configuration, never request input.
"""
import importlib.util
import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()

_TF = None  # (carn_dir, module) — cached only after a successful import


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
    d = _carn()
    if _TF is None or _TF[0] != d:
        spec = importlib.util.spec_from_file_location("carn_trie_forks", d / "scripts" / "trie_forks.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # raises before caching, so a broken import is retried
        _TF = (d, mod)
    return _TF[1]


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
        if isinstance(g, dict) and isinstance(g.get("nodes"), list):
            out.append({"file": p.name, **g})
    return out


def _run_dir(name: str) -> Path:
    """Resolve a client-supplied run name strictly inside CARN_DIR/runs/."""
    root = (_carn() / "runs").resolve()
    d = (root / name).resolve()
    if not (d.is_relative_to(root) and d != root):
        raise HTTPException(400, f"run outside runs/: {name}")
    if not (d / "trajectory.json").is_file():
        raise HTTPException(404, f"no trajectory.json in runs/{name}")
    return d


def _load_steps(d: Path) -> list:
    traj = _read_json(d / "trajectory.json")
    steps = traj.get("steps") if isinstance(traj, dict) else None
    if not isinstance(steps, list):
        raise ValueError("trajectory.json has no steps list")
    return steps


def _label(tf, d: Path):
    """True/False from the pytest summary; None when the run has no readable test output."""
    try:
        return tf.label_from_tests(str(d))
    except (OSError, ValueError):
        return None


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
            n_steps = len(_load_steps(d))
        except ValueError:
            n_steps = None  # unreadable trajectory: listed so it isn't silently hidden
        out.append({"name": str(d.relative_to(root)),
                    "passed": _label(tf, d) if n_steps is not None else None,
                    "n_steps": n_steps})
    return out


@router.get("/api/carn/runs")
def runs_endpoint():
    return _list_runs()


def _ser(root_node):
    """Iterative trie -> dict (a long single-chain run would blow the recursion limit)."""
    out = {"npass": root_node.npass, "nfail": root_node.nfail,
           "example": root_node.example, "ends": root_node.ends, "children": {}}
    stack = [(root_node, out)]
    while stack:
        src, dst = stack.pop()
        for tok, c in src.children.items():
            cd = {"npass": c.npass, "nfail": c.nfail, "example": c.example,
                  "ends": c.ends, "children": {}}
            dst["children"][tok] = cd
            stack.append((c, cd))
    return out


# Illustrative exemplar shown when no labeled runs/ exist on disk yet, so the panel
# demonstrates the mechanics instead of rendering an empty page. Nine synthetic weak-agent
# trajectories across three real task families (crack-7z, fix-git, a shell-script task),
# tuned to surface three outcome forks — where a proven route splits from a dead end, i.e.
# where a cairn belongs. Flagged demo:true; not real mined runs. The minimal 2-run fix-git
# case lives in trie_forks.py --self-test.
_EXEMPLAR = [
    # crack-7z-hash.hard — recover a secret from a password-protected archive.
    ("crack7z-guess", False, [
        "cd /app", "7z x secrets.7z -p'letmein'", "7z x secrets.7z -p'johntheRipper'"]),
    ("crack7z-no-deps", False, [
        "cd /app", "perl /app/john/run/7z2john.pl secrets.7z > /app/hash.txt"]),
    ("crack7z-install-guess", False, [
        "cd /app", "apt-get install -y p7zip-full", "7z x secrets.7z -p'admin'"]),
    ("crack7z-solve", True, [
        "cd /app", "apt-get install -y p7zip-full libcompress-raw-lzma-perl",
        "perl /app/john/run/7z2john.pl secrets.7z > /app/hash.txt",
        "john --incremental=Digits /app/hash.txt", "7z x secrets.7z -p1998",
        "cp /app/secret.txt /app/solution.txt"]),
    ("crack7z-no-deliver", False, [
        "cd /app", "apt-get install -y p7zip-full libcompress-raw-lzma-perl",
        "perl /app/john/run/7z2john.pl secrets.7z > /app/hash.txt",
        "john --incremental=Digits /app/hash.txt", "7z x secrets.7z -p1998",
        "echo 1998 > /app/answer.txt"]),
    # fix-git — restore a file mangled by a bad merge.
    ("fixgit-baseline", False, [
        "cd /app/repo", "git reflog", "git merge a82b384",
        "cat > _includes/about.md << 'ENDOFFILE'\nAbout us...", "git add -A && git commit --no-edit"]),
    ("fixgit-rescue", True, [
        "cd /app/repo", "git reflog", "git merge a82b384",
        "git checkout a82b384 -- _includes/about.md", "git commit --amend --no-edit",
        "git diff a82b384 -- _includes/about.md"]),
    # shell-script task — ship a working executable.
    ("script-noexec", False, [
        "cd /app", "vim solution.sh", "bash solution.sh"]),
    ("script-fixed", True, [
        "cd /app", "vim solution.sh", "chmod +x solution.sh", "bash solution.sh"]),
]


@router.get("/api/carn/trie")
def trie(runs: str = ""):
    tf = _tf()
    names = [r for r in runs.split(",") if r] or \
        [r["name"] for r in _list_runs() if r["n_steps"] is not None]
    if len(names) > 200:
        raise HTTPException(400, f"too many runs ({len(names)}); cap is 200")
    loaded, skipped, demo = [], [], False
    for name in names:
        d = _run_dir(name)
        try:
            steps = _load_steps(d)
        except ValueError as e:
            skipped.append({"name": name, "reason": str(e)})
            continue
        passed = _label(tf, d)
        if passed is None:
            skipped.append({"name": name, "reason": "no test output (unlabeled)"})
            continue
        loaded.append((name, passed, tf.tokenize(steps)))
    if not loaded and not names:
        demo = True
        loaded = [(n, p, tf.tokenize([{"command": c} for c in cmds]))
                  for n, p, cmds in _EXEMPLAR]
    root_node, total, nodes = tf.build(loaded)
    return {"demo": demo, "skipped": skipped,
            "runs": [{"name": n, "passed": p, "tokens": [{"tok": t, "raw": r} for t, r in ts]}
                     for n, p, ts in loaded],
            "trie": _ser(root_node),
            "stats": {"total_steps": total, "nodes": nodes, "shared": total - nodes},
            "forks": [{"path": p} for p, _ in tf.forks(root_node)]}
