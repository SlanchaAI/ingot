import os
import sys

OPENROUTER_URL = "https://openrouter.ai/api/v1"

# Zero data retention, hardcoded on every OpenRouter call (README: Privacy). Local
# OpenAI-compatible endpoints (vLLM, Ollama) don't get provider preferences — they wouldn't
# understand them, and local inference is the strongest privacy there is.
ZDR_PROVIDER = {"provider": {"zdr": True, "data_collection": "deny"}}

_KEY_HELP = """\
error: OPENROUTER_API_KEY is not set — the optimizer needs it for LLM calls.

  1. cp .env.example .env
  2. put your key in it (get one at https://openrouter.ai/keys)
  3. re-run this command

(Running fully local instead? Point MODEL_BASE_URL / OPENROUTER_BASE_URL at your vLLM or Ollama
OpenAI-compatible endpoint — no key is required then.)
"""


def model_base_url() -> str:
    """Endpoint for the serving-model role (agent runs, A/B eval agents, GEPA rollouts).
    MODEL_BASE_URL lets this role run against a local vLLM/Ollama server while the teacher and
    judge stay wherever OPENROUTER_BASE_URL points."""
    return os.environ.get("MODEL_BASE_URL") or teacher_base_url()


def teacher_base_url() -> str:
    """Endpoint for the teacher-side roles (GEPA reflection, judge, task drafting)."""
    return os.environ.get("OPENROUTER_BASE_URL") or OPENROUTER_URL


def is_openrouter(url: str) -> bool:
    return "openrouter.ai" in url


def openrouter_extra_body() -> dict:
    """Provider preferences for OpenRouter calls: the hardcoded ZDR policy, plus an optional
    allowlist (OPENROUTER_PROVIDERS=fireworks[,deepinfra] -> provider.only) for users who prefer
    one trusted vendor over pool resilience. The allowlist composes with ZDR — a pinned provider
    still must qualify as zero-data-retention."""
    provider = dict(ZDR_PROVIDER["provider"])
    only = [p.strip() for p in os.environ.get("OPENROUTER_PROVIDERS", "").split(",") if p.strip()]
    if only:
        provider["only"] = only
    return {"provider": provider}


def client_kwargs(base_url: str) -> dict:
    """ChatOpenAI connection kwargs for an endpoint. OpenRouter gets the hardcoded ZDR provider
    preference (plus the optional OPENROUTER_PROVIDERS allowlist); anything else is treated as a
    local OpenAI-compatible server — no provider preferences, and a placeholder api_key if none
    is set (the client requires one)."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if is_openrouter(base_url):
        return {"base_url": base_url, "api_key": key, "extra_body": openrouter_extra_body()}
    return {"base_url": base_url, "api_key": key or "local", "extra_body": {}}


def openrouter_key_missing() -> bool:
    """True when some active endpoint is OpenRouter and no key is configured. Fully-local
    setups (both roles pointed at vLLM/Ollama) never need a key."""
    urls = {model_base_url(), teacher_base_url()}
    return any(is_openrouter(u) for u in urls) and not os.environ.get("OPENROUTER_API_KEY", "").strip()


def require_openrouter_key() -> None:
    """Friendly preflight for the CLI entrypoints: exit with setup help instead of a mid-run 401,
    and catch pin/model conflicts before any tokens are spent."""
    if openrouter_key_missing():
        print(_KEY_HELP, file=sys.stderr)
        raise SystemExit(1)
    preflight_provider_pins()


def _normalize(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def provider_conflict(model: str, pins: list[str]) -> str | None:
    """None if some pinned provider serves `model` per OpenRouter's public endpoints API;
    otherwise a human-readable explanation. Network problems return None (fail open — the
    preflight is advice, not a gate on offline work)."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"https://openrouter.ai/api/v1/models/{model}/endpoints", timeout=10) as r:
            endpoints = json.loads(r.read()).get("data", {}).get("endpoints", [])
    except Exception:
        return None
    if not endpoints:
        return (f"model '{model}' has no endpoints on OpenRouter — check the model id "
                f"(https://openrouter.ai/models)")
    served_by = [e.get("provider_name", "") for e in endpoints]
    normalized = {_normalize(p) for p in served_by}
    if any(_normalize(pin) in n or n in _normalize(pin) for pin in pins for n in normalized if n):
        return None
    return (f"OPENROUTER_PROVIDERS={','.join(pins)} pins providers that don't serve '{model}' "
            f"(served by: {', '.join(sorted(set(served_by)))}). Fix OPENROUTER_PROVIDERS, or pick "
            f"a model your pinned provider offers (https://openrouter.ai/{model}).")


def preflight_provider_pins() -> None:
    """When OPENROUTER_PROVIDERS is set, verify every role that talks to OpenRouter uses a model
    the pinned providers actually serve — exit with the explanation instead of a mid-run 404.
    (Even a served model can still fail at call time if the pinned provider isn't ZDR-qualified
    for it; that error is caught and explained by the runtime handler in optimize/judge.py.)"""
    pins = [p.strip() for p in os.environ.get("OPENROUTER_PROVIDERS", "").split(",") if p.strip()]
    if not pins:
        return
    roles = {}
    if is_openrouter(model_base_url()):
        roles["MODEL"] = os.environ.get("MODEL", "qwen/qwen3.6-27b")
    if is_openrouter(teacher_base_url()):
        roles["GEPA_MODEL"] = os.environ.get("GEPA_MODEL", "z-ai/glm-5.2")
        judges = os.environ.get("JUDGE_MODELS", os.environ.get("JUDGE_MODEL", "google/gemini-2.5-flash"))
        for i, judge in enumerate(m.strip() for m in judges.split(",") if m.strip()):
            roles[f"JUDGE_MODEL[{i}]"] = judge
    problems = []
    for role, model in sorted(set(roles.items())):
        conflict = provider_conflict(model, pins)
        if conflict:
            problems.append(f"  {role}={model}: {conflict}")
    if problems:
        # SystemExit with a message: exits 1 with the text on stderr for CLIs, and the UI
        # endpoint re-surfaces str(e) as a 400 detail.
        raise SystemExit("error: provider pin conflicts detected before spending any tokens:\n"
                         + "\n".join(problems))
