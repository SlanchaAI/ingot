"""Per-skill load counter: how often each skill's instructions were actually served, a `get_skill`
load or a `route_and_load` match (a `suggest_skills` impression does not count). The MCP server
increments it; the review UI reads it. Persisted to runs/skill_usage.json. Best-effort throughout:
a counter failure must never break serving."""
import json
import os
import threading
from pathlib import Path

_LOCK = threading.Lock()
_PATH = Path(os.environ.get("SKILL_USAGE_FILE") or
             Path(__file__).resolve().parent.parent / "runs" / "skill_usage.json")


def load_counts() -> dict[str, int]:
    """{skill_name: load_count}; {} if unwritten or unreadable."""
    try:
        data = json.loads(_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_use(name: str) -> None:
    """Increment one skill's load count, atomically (temp file + rename). Swallows all IO errors."""
    if not name:
        return
    with _LOCK:
        counts = load_counts()
        counts[name] = int(counts.get(name, 0)) + 1
        try:
            _PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _PATH.with_name(_PATH.name + ".tmp")
            tmp.write_text(json.dumps(counts))
            tmp.replace(_PATH)
        except OSError:
            pass  # counting is best-effort; never break the serving path
