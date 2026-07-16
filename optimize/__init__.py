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
    """Friendly preflight for the CLI entrypoints: exit with setup help instead of a mid-run 401."""
    if openrouter_key_missing():
        print(_KEY_HELP, file=sys.stderr)
        raise SystemExit(1)
