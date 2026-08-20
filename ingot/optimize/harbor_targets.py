"""Identity and safe routing helpers for Harbor's local model targets.

Endpoint addresses are runtime inputs. They are retained only on the immutable target used to make
the request; the stable job identity uses a fingerprint of the normalized address and served model.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Mapping


MIN_CONTEXT_LENGTH = 32_768
PROTOCOLS = frozenset({"chat", "responses", "messages"})
PROTOCOL_PATHS = {
    "chat": "/v1/chat/completions",
    "responses": "/v1/responses",
    "messages": "/v1/messages",
}

# This is the complete local matrix allowlist. Keep it in one mapping so routing cannot silently
# fall back to a provider's default protocol when a new Harbor adapter is added.
HARNESS_PROTOCOLS = {
    "claude-code": "messages",
    "terminus-2": "chat",
    "goose": "chat",
    "opencode": "chat",
    "openclaw": "chat",
    "mini-swe-agent": "chat",
    "codex": "responses",
    "aider": "chat",
    "pi": "chat",
}

# Alias configuration pins served identity and display/scale provenance. Context length alone comes
# from discovery; parse_target uses the minimum accepted value until that live check completes.
TARGETS = {
    "dell-qwen": {"display_name": "Qwen/Qwen3.6-27B", "served_model": "dot-backbone",
                  "family": "Qwen3.6", "parameter_billions": 27.0,
                  "quantization": "fp8-published", "tool_parser": "qwen3_xml"},
    # Alias names avoid punctuation; the server's exact model ID retains the decimal point.
    "qwen35-08b": {"display_name": "Qwen/Qwen3.5-0.8B", "served_model": "qwen35-0.8b",
                    "family": "Qwen3.5", "parameter_billions": 0.8,
                    "quantization": "fp8-load", "tool_parser": "qwen3_coder"},
    "qwen35-2b": {"display_name": "Qwen/Qwen3.5-2B", "served_model": "qwen35-2b",
                   "family": "Qwen3.5", "parameter_billions": 2.0,
                   "quantization": "fp8-load", "tool_parser": "qwen3_coder"},
    "qwen35-4b": {"display_name": "Qwen/Qwen3.5-4B", "served_model": "qwen35-4b",
                   "family": "Qwen3.5", "parameter_billions": 4.0,
                   "quantization": "fp8-load", "tool_parser": "qwen3_coder"},
    "qwen35-9b": {"display_name": "Qwen/Qwen3.5-9B", "served_model": "qwen35-9b",
                   "family": "Qwen3.5", "parameter_billions": 9.0,
                   "quantization": "fp8-load", "tool_parser": "qwen3_coder"},
    "orin-qwen35-9b": {"display_name": "Qwen/Qwen3.5-9B (Q4_K_M)",
                         "served_model": "qwen3.5:9b", "family": "Qwen3.5",
                         "parameter_billions": 9.7, "quantization": "Q4_K_M",
                         "tool_parser": "ollama", "context_api": "ollama-ps"},
    "spark-deepseek": {"display_name": "deepseek-ai/DeepSeek-V4-Flash-0731 (NVFP4)", "served_model": "deepseek-v4-flash",
                       "family": "DeepSeek V4 Flash", "parameter_billions": None,
                       "quantization": "nvfp4", "tool_parser": "deepseek_v3"},
    "orin-abliterated": {"display_name": "ablit35b (34.66B, Q4_K_M)", "served_model": "ablit35b",
                         "family": "Abliterated 35B", "parameter_billions": 35.0,
                         "quantization": "gguf", "tool_parser": "llama.cpp"},
}

_PROVIDER_PREFIXES = (
    "ANTHROPIC_",
    "CLAUDE_",
    "OPENAI_",
    "OPENROUTER_",
    "CODEX_",
    "LITELLM_",
    "GEMINI_",
    "GOOSE_",
    "AIDER_",
    "OPENCODE_",
    "OPENCLAW_",
    "MINI_SWE_AGENT_",
    "PI_",
)
_EXACT_SECRET_KEYS = frozenset({"API_KEY", "MODEL_API_KEY"})


def _canonical_url(base_url: str) -> str:
    value = str(base_url).strip()
    if value.endswith("/"):
        value = value[:-1]
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base URL must be an http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base URL must not contain a query or fragment")
    return value


def _alias_config(alias: str) -> dict[str, str]:
    try:
        return TARGETS[alias]
    except KeyError as exc:
        raise ValueError(f"unknown local target alias: {alias}") from exc


@dataclass(frozen=True)
class LocalTarget:
    alias: str
    display_name: str
    base_url: str
    served_model: str
    context_length: int
    protocols: frozenset[str]
    family: str = ""
    parameter_billions: float | None = None
    quantization: str = ""
    tool_parser: str = ""

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {"base_url": _canonical_url(self.base_url), "served_model": self.served_model},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()[:12]

    @property
    def job_slug(self) -> str:
        return f"{self.alias}-{self.fingerprint}"


def parse_target(spec: str) -> LocalTarget:
    """Parse ``alias=base_url`` into a provisional configured target."""
    alias, separator, base_url = str(spec).partition("=")
    if not separator or not alias.strip() or not base_url.strip():
        raise ValueError("target must be specified as ALIAS=BASE_URL")
    config = _alias_config(alias.strip())
    return LocalTarget(
        alias=alias.strip(),
        display_name=config["display_name"],
        base_url=_canonical_url(base_url),
        served_model=config["served_model"],
        context_length=MIN_CONTEXT_LENGTH,
        protocols=PROTOCOLS,
        family=config["family"],
        parameter_billions=config["parameter_billions"],
        quantization=config["quantization"],
        tool_parser=config["tool_parser"],
    )


def _ollama_runtime_context(base_url: str, served_model: str, timeout: float) -> object:
    """Read the loaded Ollama runtime context when its OpenAI model list omits that field."""
    request = urllib.request.Request(f"{base_url}/api/ps", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is not None and not 200 <= int(status) < 300:
                raise RuntimeError(f"Ollama process list returned HTTP {status}")
            payload = json.loads(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(f"Ollama process list failed: {exc}") from exc
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None
    loaded = next((item for item in models
                   if isinstance(item, dict) and item.get("name") == served_model), None)
    return loaded.get("context_length") if loaded else None


def discover_target(alias: str, base_url: str, *, timeout: float = 10.0) -> LocalTarget:
    """Fetch ``/v1/models`` and return a target after identity/context validation."""
    config = _alias_config(alias)
    canonical = _canonical_url(base_url)
    request = urllib.request.Request(f"{canonical}/v1/models", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is not None and int(status) >= 400:
                raise RuntimeError(f"model discovery returned HTTP {status}")
            payload = json.loads(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("model discovery"):
            raise
        raise RuntimeError(f"model discovery failed for {alias}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("model discovery returned an invalid response object")
    model = next((entry for entry in payload["data"]
                  if isinstance(entry, dict) and entry.get("id") == config["served_model"]), None)
    if model is None:
        ids = [entry.get("id") for entry in payload["data"] if isinstance(entry, dict)]
        raise ValueError(f"served model {config['served_model']!r} not found; received {ids!r}")
    context = model.get("max_model_len", model.get("context_length"))
    if context is None and isinstance(model.get("meta"), dict):
        context = model["meta"].get("n_ctx")
    if context is None and config.get("context_api") == "ollama-ps":
        context = _ollama_runtime_context(canonical, config["served_model"], timeout)
    if not isinstance(context, int) or isinstance(context, bool):
        raise ValueError(f"served model {config['served_model']!r} has no context length")
    if context < MIN_CONTEXT_LENGTH:
        raise ValueError(f"served model {config['served_model']!r} context length {context} is below "
                         f"the minimum {MIN_CONTEXT_LENGTH}")
    return LocalTarget(
        alias=alias,
        display_name=config["display_name"],
        base_url=canonical,
        served_model=config["served_model"],
        context_length=context,
        protocols=PROTOCOLS,
        family=config["family"],
        parameter_billions=config["parameter_billions"],
        quantization=config["quantization"],
        tool_parser=config["tool_parser"],
    )


def protocol_for(harness: str) -> str:
    try:
        return HARNESS_PROTOCOLS[harness]
    except KeyError as exc:
        raise ValueError(f"unsupported harness for local target: {harness}") from exc


def harbor_model(target: LocalTarget, harness: str) -> str:
    protocol_for(harness)
    # Harbor's local CLI adapters use provider/model identifiers; Claude Code consumes the
    # Anthropic-compatible model ID directly.
    if harness == "claude-code":
        return target.served_model
    if harness == "opencode":
        return f"local/{target.served_model}"
    return f"openai/{target.served_model}"


def harbor_agent_kwargs(target: LocalTarget, harness: str) -> dict[str, str]:
    """Return only adapter kwargs that cannot be supplied through the child environment."""
    protocol_for(harness)
    if harness == "terminus-2":
        return {"api_base": f"{_canonical_url(target.base_url)}/v1"}
    if harness == "openclaw":
        # Harbor defaults this adapter to "high", which both local models reject.
        return {"thinking": "off"}
    if harness == "opencode":
        model = target.served_model
        # OpenCode otherwise requests 32k output tokens. Reserve three quarters of the
        # discovered context for its prompt, tool calls, and accumulated transcript.
        output_limit = target.context_length // 4
        return {"opencode_config": {"provider": {"local": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": f"{_canonical_url(target.base_url)}/v1", "apiKey": "local"},
            "models": {model: {"limit": {"context": target.context_length,
                                         "output": output_limit}}},
        }}}}
    return {}


def scrub_provider_env(parent: Mapping[str, str]) -> dict[str, str]:
    """Copy a parent environment without provider credentials or routing controls."""
    return {key: value for key, value in parent.items()
            if not key.startswith(_PROVIDER_PREFIXES) and key not in _EXACT_SECRET_KEYS}


def local_agent_env(target: LocalTarget, harness: str) -> dict[str, str]:
    """Build a complete child environment with a literal local credential sentinel."""
    protocol = protocol_for(harness)
    # This mapping is repeated as Harbor --ae values. Do not copy ambient process
    # variables into it: only local sentinels and routing settings belong here.
    env: dict[str, str] = {}
    base_url = _canonical_url(target.base_url)
    openai_base_url = f"{base_url}/v1"
    # Keep every provider key at the non-secret sentinel. Adapters choose their own protocol
    # below, but a generic adapter must never discover a real inherited key and fall back to it.
    env.update({
        "ANTHROPIC_API_KEY": "local",
        "OPENAI_API_KEY": "local",
        "CODEX_API_KEY": "local",
    })
    if protocol == "messages":
        env.update({
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_MODEL": target.served_model,
        })
    elif protocol == "responses":
        env.update({
            "OPENAI_BASE_URL": openai_base_url,
            "OPENAI_API_BASE": openai_base_url,
            "OPENAI_HOST": base_url,
        })
    else:
        env.update({
            "OPENAI_BASE_URL": openai_base_url,
            "OPENAI_API_BASE": openai_base_url,
            # Goose reads OPENAI_HOST rather than the OpenAI SDK spelling.
            "OPENAI_HOST": base_url,
        })
    return env


def probe_protocol(target: LocalTarget, protocol: str, *, timeout: float = 20.0) -> None:
    """Send a one-token request and require a successful, non-empty JSON object response."""
    if protocol not in PROTOCOL_PATHS:
        raise ValueError(f"unsupported protocol: {protocol}")
    if protocol not in target.protocols:
        raise ValueError(f"target does not support protocol: {protocol}")
    body: dict[str, object]
    headers = {"Content-Type": "application/json", "Authorization": "Bearer local"}
    if protocol == "messages":
        body = {"model": target.served_model, "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}]}
        headers["anthropic-version"] = "2023-06-01"
        headers["x-api-key"] = "local"
    elif protocol == "responses":
        body = {"model": target.served_model, "input": "ping", "max_output_tokens": 1}
    else:
        body = {"model": target.served_model, "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1}
        if target.family.startswith("Qwen"):
            body["messages"] = [{"role": "user", "content": "ping /no_think"}]
    request = urllib.request.Request(
        f"{_canonical_url(target.base_url)}{PROTOCOL_PATHS[protocol]}",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is not None and not 200 <= int(status) < 300:
                raise RuntimeError(f"{protocol} probe returned HTTP {status}")
            payload = json.loads(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(protocol + " probe returned"):
            raise
        raise RuntimeError(f"{protocol} probe failed: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise RuntimeError(f"{protocol} probe returned an empty response object; expected non-empty")


def probe_chat_tool_round_trip(target: LocalTarget, *, timeout: float = 20.0) -> None:
    """Require one parsed function call and a successful tool-result continuation."""
    url = f"{_canonical_url(target.base_url)}{PROTOCOL_PATHS['chat']}"
    headers = {"Content-Type": "application/json", "Authorization": "Bearer local"}
    tool = {
        "type": "function",
        "function": {
            "name": "ingot_echo",
            "description": "Return the supplied string unchanged.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        },
    }

    def post(body: dict[str, object]) -> dict:
        request = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", None)
                if status is not None and not 200 <= int(status) < 300:
                    raise RuntimeError(f"chat tool probe returned HTTP {status}")
                payload = json.loads(response.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
                ValueError) as exc:
            raise RuntimeError(f"chat tool probe failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("chat tool probe returned an invalid response object")
        return payload

    common = {
        "model": target.served_model,
        "chat_template_kwargs": {"enable_thinking": False},
        "reasoning_effort": "none",
        "temperature": 0,
    }
    first = post({
        **common,
        "messages": [
            {"role": "system", "content": "Call ingot_echo with value cutover-ok. /no_think"},
            {"role": "user", "content": "Use the tool now."},
        ],
        "tools": [tool],
        "tool_choice": "required",
        "max_tokens": 128,
    })
    try:
        assistant = first["choices"][0]["message"]
        call = assistant["tool_calls"][0]
        arguments = json.loads(call["function"]["arguments"])
        if call["function"]["name"] != "ingot_echo" or arguments != {"value": "cutover-ok"}:
            raise ValueError
        call_id = call["id"]
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("chat tool probe did not return the required parsed tool call") from exc

    assistant_turn = {
        "role": "assistant",
        "content": assistant.get("content") or "",
        "tool_calls": assistant["tool_calls"],
    }
    second = post({
        **common,
        "messages": [
            {"role": "system", "content":
             "After the tool result, reply exactly cutover-ok. /no_think"},
            {"role": "user", "content": "Use the tool now."},
            assistant_turn,
            {"role": "tool", "tool_call_id": call_id, "content": "cutover-ok"},
        ],
        "tools": [tool],
        "max_tokens": 64,
    })
    try:
        content = second["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("chat tool probe returned an invalid continuation") from exc
    if str(content).strip() != "cutover-ok":
        raise RuntimeError("chat tool probe did not consume the tool result")
