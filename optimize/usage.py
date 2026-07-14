"""Token ledger for an optimize run: every LLM call is attributed to a role
(rollout / judge / reflection / agent_ab) so the run can report what it actually cost."""
import threading
from collections import defaultdict

COUNTS: dict[str, dict[str, int]] = defaultdict(lambda: {"input": 0, "output": 0, "calls": 0})
_LOCK = threading.Lock()  # GEPA fans rollout+judge across a thread pool


def reset():
    """Start a fresh ledger — the UI process runs many optimizations; counts must not leak across runs."""
    with _LOCK:
        COUNTS.clear()


def add(role: str, usage: dict | None):
    """usage: langchain usage_metadata ({'input_tokens','output_tokens'}) or equivalent dict."""
    if not usage:
        return
    with _LOCK:
        c = COUNTS[role]
        c["input"] += int(usage.get("input_tokens", 0))
        c["output"] += int(usage.get("output_tokens", 0))
        c["calls"] += 1


def report() -> dict:
    out = {role: dict(c) for role, c in COUNTS.items()}
    out["total"] = {
        "input": sum(c["input"] for c in COUNTS.values()),
        "output": sum(c["output"] for c in COUNTS.values()),
        "calls": sum(c["calls"] for c in COUNTS.values()),
    }
    return out


def format_report() -> str:
    r = report()
    lines = [f"  {role:<12} {c['calls']:>4} calls  {c['input']:>9,} in  {c['output']:>8,} out"
             for role, c in r.items() if role != "total"]
    t = r["total"]
    lines.append(f"  {'TOTAL':<12} {t['calls']:>4} calls  {t['input']:>9,} in  {t['output']:>8,} out")
    return "\n".join(lines)
