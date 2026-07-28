"""Token ledger for an optimize run: every LLM call is attributed to a role
(rollout / judge / reflection / agent_ab) so the run can report what it actually cost ,
including a best-effort USD estimate from OpenRouter list prices, and an optional hard
spend cap (MAX_RUN_USD) that aborts a run before it exceeds the budget."""
import os
import threading
from collections import defaultdict

COUNTS: dict[str, dict[str, int]] = defaultdict(lambda: {"input": 0, "output": 0, "calls": 0})
_LOCK = threading.RLock()  # the search fans rollout+judge across a thread pool; add() re-enters for the cap
_PRICES: dict[str, tuple[float, float]] | None = None


def reset():
    """Start a fresh ledger, the UI process runs many optimizations; counts must not leak across runs."""
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
    _enforce_cap()


def _enforce_cap():
    cap = float(os.environ.get("MAX_RUN_USD", "0") or 0)
    if not cap:
        return
    cost = estimated_cost()
    if cost is not None and cost > cap:
        raise SystemExit(f"MAX_RUN_USD exceeded: estimated ${cost:.2f} > cap ${cap:.2f}, "
                         f"aborting before spending more.\n{format_report()}")


def _openrouter_prices() -> dict[str, tuple[float, float]]:
    """model id -> (prompt, completion) USD per token from OpenRouter's public models API;
    {} on any failure (cost reporting is best-effort, never a gate on offline work)."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=10) as r:
            data = json.loads(r.read())["data"]
        return {m["id"]: (float(m["pricing"]["prompt"]), float(m["pricing"]["completion"]))
                for m in data if m.get("pricing")}
    except Exception:
        return {}


def _role_models() -> dict[str, str]:
    """Which model each ledger role runs on (first judge only, for ensemble setups)."""
    from . import agent_model, skillopt_model
    teacher = skillopt_model()
    judge = (os.environ.get("JUDGE_MODELS") or
             os.environ.get("JUDGE_MODEL", "google/gemini-2.5-flash")).split(",")[0].strip()
    return {"rollout": agent_model(), "agent_ab": agent_model(),
            "judge": judge, "reflection": teacher}


def _model_for(role: str) -> str:
    """The model a ledger role ran on. A role may name its own model after a colon
    (`compat:anthropic/claude-sonnet-4.5`): the compatibility sweep varies the *serving* model by
    design, so unlike the fixed roles it cannot be mapped to one model up front."""
    name, _, explicit = role.partition(":")
    return explicit or _role_models().get(name, "")


def unpriced_roles() -> list[str]:
    """Roles that spent tokens but contribute nothing to the estimate, because their model carries
    no OpenRouter list price — a local endpoint (genuinely free) or a slug we could not resolve
    (not free at all). Surfaced rather than swallowed: an unpriced role silently counts as $0, and
    that is how the entire compatibility sweep once vanished from both the cost line and the
    MAX_RUN_USD cap, reporting $0.04 against $1.42 actually spent."""
    if _PRICES is None:
        return []
    with _LOCK:
        return sorted(role for role, c in COUNTS.items()
                      if (c["input"] or c["output"]) and not _PRICES.get(_model_for(role)))


def estimated_cost() -> float | None:
    """Best-effort USD estimate for the current ledger, from OpenRouter list prices. None when
    the endpoint isn't OpenRouter or pricing is unavailable (local endpoints cost nothing).
    Roles whose model has no list price contribute nothing — ask unpriced_roles() which those are
    before trusting this as a total."""
    global _PRICES
    from . import is_openrouter, teacher_base_url
    if not is_openrouter(teacher_base_url()):
        return None
    if _PRICES is None:
        _PRICES = _openrouter_prices()
    if not _PRICES:
        return None
    with _LOCK:
        return sum(c["input"] * p[0] + c["output"] * p[1]
                   for role, c in COUNTS.items()
                   if (p := _PRICES.get(_model_for(role))))


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
    cost = estimated_cost()
    if cost is not None:
        lines.append(f"  estimated cost: ${cost:.2f} (OpenRouter list prices)")
        if (blind := unpriced_roles()):
            lines.append(f"  NOT in that estimate: {', '.join(blind)} — no list price for "
                         f"{', '.join(sorted({_model_for(r) or '?' for r in blind}))}. "
                         f"Free if that is a local endpoint; otherwise the figure above is low, "
                         f"and so is any MAX_RUN_USD cap resting on it.")
    return "\n".join(lines)
