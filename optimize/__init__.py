import os
import sys

OPENROUTER_URL = "https://openrouter.ai/api/v1"

# Zero data retention, hardcoded on every OpenRouter call (README: Privacy). Local
# OpenAI-compatible endpoints (vLLM, Ollama) don't get provider preferences — they wouldn't
# understand them, and local inference is the strongest privacy there is.
ZDR_PROVIDER = {"provider": {"zdr": True, "data_collection": "deny"}}

_KEY_HELP = """\
error: no API key is set for your LLM endpoint, and candidate generation needs one.

  1. cp .env.example .env
  2. put your key in it (OpenRouter: https://openrouter.ai/keys — or set BASE_URL + API_KEY for
     any other OpenAI-compatible provider, e.g. Fireworks)
  3. re-run this command

(Running fully local instead? Point BASE_URL / MODEL_BASE_URL at your vLLM or Ollama
OpenAI-compatible endpoint — no key is required then.)
"""


def agent_model() -> str:
    """The serving/agent model — everything that *executes* skills (agent runs, A/B eval agents,
    candidate rollouts). AGENT_MODEL wins; MODEL is the legacy alias so existing .env files keep
    working."""
    return os.environ.get("AGENT_MODEL") or os.environ.get("MODEL") or "qwen/qwen3.6-27b"


def model_base_url() -> str:
    """Endpoint for the serving-model role (agent runs, A/B eval agents, candidate rollouts).
    MODEL_BASE_URL lets this role run against a different provider (local vLLM/Ollama, or e.g.
    Fireworks direct) while the teacher and judge stay wherever BASE_URL points."""
    return os.environ.get("MODEL_BASE_URL") or teacher_base_url()


def teacher_base_url() -> str:
    """Endpoint for the teacher-side roles (candidate authoring, reflection, judge, task drafting).
    Generic BASE_URL wins (any OpenAI-compatible provider); OPENROUTER_BASE_URL is the legacy
    alias."""
    return (os.environ.get("BASE_URL") or os.environ.get("OPENROUTER_BASE_URL") or OPENROUTER_URL)


def api_key() -> str:
    """Bearer token for the configured endpoint. Generic API_KEY wins; OPENROUTER_API_KEY is the
    legacy fallback so existing .env files keep working."""
    return os.environ.get("API_KEY", "") or os.environ.get("OPENROUTER_API_KEY", "")


def model_api_key() -> str:
    """Key for the serving-model endpoint (falls back to the shared key) — hybrid setups can use
    a different vendor for the serving role."""
    return os.environ.get("MODEL_API_KEY", "") or api_key()


def is_openrouter(url: str) -> bool:
    return "openrouter.ai" in url


def langfuse_available() -> bool:
    """True when the configured Langfuse answers its health endpoint (3s probe). The A/B gate
    and the miner use this to choose between experiment-logged runs and the local lite path."""
    import urllib.request
    base = os.environ.get("LANGFUSE_BASE_URL", "http://langfuse-web:3000").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/public/health", timeout=3):
            return True
    except OSError:
        return False


def traces_file():
    """Local JSONL trace store: the zero-infrastructure fallback written by the agent and read
    by optimize-mine when the Langfuse stack isn't running (one {task, answer, tags} per line)."""
    from pathlib import Path
    return Path(os.environ.get("TRACES_FILE") or
                Path(__file__).resolve().parent.parent / "runs" / "traces.jsonl")


def openrouter_extra_body() -> dict:
    """Provider preferences for OpenRouter calls: the hardcoded ZDR policy, plus an optional
    priority list (OPENROUTER_PROVIDERS=fireworks[,groq] -> provider.order): listed providers
    are tried first, in order, and models none of them serves fall back to the rest of the
    pool. The priority composes with ZDR: a listed provider still must qualify as
    zero-data-retention for the model, or routing skips it."""
    provider = dict(ZDR_PROVIDER["provider"])
    order = [p.strip() for p in os.environ.get("OPENROUTER_PROVIDERS", "").split(",") if p.strip()]
    if order:
        provider["order"] = order
    return {"provider": provider}


def client_kwargs(base_url: str, key: str | None = None, reasoning: bool = False) -> dict:
    """ChatOpenAI connection kwargs for an endpoint. OpenRouter gets the hardcoded ZDR provider
    preference (plus the optional OPENROUTER_PROVIDERS allowlist); any other OpenAI-compatible
    endpoint (Fireworks/Together direct, local vLLM/Ollama) gets no provider preferences and a
    placeholder api_key if none is set (the client requires one). reasoning=True pins OpenRouter's
    unified reasoning parameter on rather than relying on per-model defaults; local endpoints
    ignore it (thinking is a server-side setting there)."""
    key = key if key is not None else api_key()
    if is_openrouter(base_url):
        extra = openrouter_extra_body()
        if reasoning:
            extra["reasoning"] = {"enabled": True}
        return {"base_url": base_url, "api_key": key, "extra_body": extra}
    return {"base_url": base_url, "api_key": key or "local", "extra_body": {}}


def openrouter_key_missing() -> bool:
    """True when a hosted (https) endpoint is in use with no key configured. Local http endpoints
    (vLLM/Ollama) never need one; each role checks the key that would actually be sent for it."""
    if teacher_base_url().startswith("https://") and not api_key().strip():
        return True
    return model_base_url().startswith("https://") and not model_api_key().strip()


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
    return (f"OPENROUTER_PROVIDERS={','.join(pins)} lists no provider that serves '{model}' "
            f"(served by: {', '.join(sorted(set(served_by)))}); this role falls back to the "
            f"open ZDR pool (https://openrouter.ai/{model}).")


def preflight_provider_pins() -> list[str]:
    """When OPENROUTER_PROVIDERS is set, check which OpenRouter-facing roles use a model no
    listed provider serves. With priority semantics those roles simply fall back to the open
    ZDR pool, so this warns instead of exiting; the warnings are also returned for callers/tests.
    (A served model can still be skipped at call time if the provider isn't ZDR-qualified for
    it; that error is caught and explained by the runtime handler in optimize/judge.py.)"""
    pins = [p.strip() for p in os.environ.get("OPENROUTER_PROVIDERS", "").split(",") if p.strip()]
    if not pins:
        return []
    roles = {}
    if is_openrouter(model_base_url()):
        roles["AGENT_MODEL"] = agent_model()
    if is_openrouter(teacher_base_url()):
        roles["GEPA_MODEL"] = os.environ.get("GEPA_MODEL", "z-ai/glm-5.2")
        if os.environ.get("STRONG_MODEL"):  # default is GEPA_MODEL, already checked above
            roles["STRONG_MODEL"] = os.environ["STRONG_MODEL"]
        judges = os.environ.get("JUDGE_MODELS", os.environ.get("JUDGE_MODEL", "google/gemini-2.5-flash"))
        for i, judge in enumerate(m.strip() for m in judges.split(",") if m.strip()):
            roles[f"JUDGE_MODEL[{i}]"] = judge
    problems = []
    for role, model in sorted(set(roles.items())):
        conflict = provider_conflict(model, pins)
        if conflict:
            problems.append(f"  {role}={model}: {conflict}")
    if problems:
        print("warning: some roles are not covered by OPENROUTER_PROVIDERS and will fall back "
              "to the open ZDR pool:\n" + "\n".join(problems), file=sys.stderr)
    return problems


# The serving contract: how a skill body is presented to the model that executes it. The quality
# A/B serves variants with exactly this template, and the candidate search's rollouts optimize
# against the same text: search and A/B must never disagree about the contract.
SERVE_TEMPLATE = """You are a deep agent serving a user request. The following skill has been
loaded for this task — follow its instructions. Keep the final answer concise.
Your final answer must contain the complete deliverable itself — e.g. full runnable code inline —
never just a description of, or reference to, files you created in your workspace: the user cannot
see your workspace.

# Loaded skill
{body}"""
