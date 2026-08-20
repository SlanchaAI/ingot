"""Narrow, runner-owned LiteLLM compatibility gateway for observed role rejections.

The gateway is deliberately not a general provider proxy.  It exists only while Harbor evaluates
the three local harness/target combinations whose native endpoints rejected system/developer
roles.  Its model list has no fallback deployment or provider credential.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .harbor_targets import LocalTarget, scrub_provider_env


# Docker's bridge gateway is reachable from Harbor's per-trial bridge networks but not published
# outside the Dell host.  A fixed address/port makes it possible to health-check before Harbor.
GATEWAY_HOST = "172.17.0.1"
GATEWAY_PORT = 4865
GATEWAY_URL = f"http://{GATEWAY_HOST}:{GATEWAY_PORT}"
GATEWAY_REVISION = "litellm-1.93-role-user-v1"
DELL_CLAUDE_OUTPUT_CAP_REVISION = "litellm-1.93-role-user-output-v4"
DELL_CODEX_HTTP_REVISION = "litellm-1.93-role-user-codex-http-catalog-v8"
# This is an ignored Dell-only environment, created from requirements-harbor-gateway.txt.  Harbor
# and Ingot's primary environment deliberately remain untouched.
_LITELLM_BIN = str(Path(__file__).resolve().parents[2] / ".venv-harbor-gateway/bin/litellm")
_CLAUDE_GATEWAY_TARGETS = frozenset({"dell-qwen", "spark-deepseek"})


@dataclass(frozen=True)
class GatewayRoute:
    harness: str
    target_alias: str
    model: str
    served_model: str
    upstream_env: str
    output_limit: int | None = None
    revision: str = GATEWAY_REVISION

    @property
    def identity(self) -> str:
        payload = json.dumps(
            {"harness": self.harness, "target": self.target_alias, "model": self.model,
             "served_model": self.served_model, "output_limit": self.output_limit,
             "revision": self.revision}, sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(payload).hexdigest()[:12]


def gateway_route(target: LocalTarget, harness: str) -> GatewayRoute | None:
    """Return a route only for the three observed local role-protocol failures."""
    if ((harness == "claude-code" and target.alias in _CLAUDE_GATEWAY_TARGETS)
            or (harness == "codex" and target.alias == "dell-qwen")):
        # Put the translation revision in the gateway's served model name.  A surviving process
        # from an old run then fails the model-list health check instead of silently reusing
        # evidence made under a different role conversion.
        output_limit = None
        revision = GATEWAY_REVISION
        if harness == "claude-code" and target.alias == "dell-qwen":
            # A 1/4 cap left a 24,577-token trajectory one token beyond this target's 32,768
            # window. Reserve seven eighths for the accumulated prompt and tool history instead.
            output_limit = target.context_length // 8
            revision = f"{DELL_CLAUDE_OUTPUT_CAP_REVISION}-{output_limit}"
        elif harness == "codex":
            revision = DELL_CODEX_HTTP_REVISION
        revision_hash = hashlib.sha256(revision.encode()).hexdigest()[:8]
        slug = f"harbor-compat-{target.alias}-{harness}-{revision_hash}"
        return GatewayRoute(harness, target.alias, slug, target.served_model,
                            f"HARBOR_GATEWAY_UPSTREAM_{target.alias.upper().replace('-', '_')}",
                            output_limit=output_limit, revision=revision)
    return None


def _system_user(content: Any, label: str) -> dict[str, Any]:
    if isinstance(content, str):
        content = f"[{label}]\n{content}"
    return {"role": "user", "content": content}


def normalize_role_request(data: dict[str, Any], call_type: str,
                           output_limits: dict[str, int] | None = None) -> dict[str, Any]:
    """Convert only roles rejected by the local endpoints, retaining every tool object verbatim."""
    data = dict(data)
    if call_type == "anthropic_messages":
        messages = list(data.get("messages") or [])
        system = data.pop("system", None)
        if system not in (None, "", []):
            messages.insert(0, _system_user(system, "system"))
        data["messages"] = [
            _system_user(message.get("content"), message.get("role"))
            if isinstance(message, dict) and message.get("role") in {"system", "developer"}
            else message
            for message in messages
        ]
    elif call_type == "aresponses":
        items = list(data.get("input") or [])
        instructions = data.pop("instructions", None)
        if instructions not in (None, "", []):
            items.insert(0, _system_user(
                [{"type": "input_text", "text": f"[instructions]\n{instructions}"}], "instructions"))
        data["input"] = [
            {**item, "role": "user"}
            if isinstance(item, dict) and item.get("role") in {"system", "developer"}
            else item
            for item in items
        ]
        # Codex attaches its model-default reasoning object even when Harbor omits the CLI flag.
        # LiteLLM's Responses bridge derives the custom backend's rejected reasoning_effort from it.
        data.pop("reasoning", None)
        data.pop("reasoning_effort", None)
    # LiteLLM 1.93's custom_openai Chat Completions route never accepts this Responses-style
    # key. Every Claude Messages request uses that route; only Dell gets a max_tokens cap.
    if call_type == "anthropic_messages":
        data.pop("max_output_tokens", None)
    output_limit = (output_limits or {}).get(str(data.get("model")))
    if output_limit is not None:
        keys = ("max_tokens",) if call_type == "anthropic_messages" else ("max_tokens", "max_output_tokens")
        for key in keys:
            value = data.get(key)
            if value is None or isinstance(value, int) and value > output_limit:
                data[key] = output_limit
    return data


def gateway_agent_env(target: LocalTarget, route: GatewayRoute) -> dict[str, str]:
    """Explicit Harbor child settings for a gateway route, never a provider key."""
    env = {"ANTHROPIC_API_KEY": "local", "OPENAI_API_KEY": "local", "CODEX_API_KEY": "local"}
    if route.harness == "claude-code":
        env.update({"ANTHROPIC_BASE_URL": GATEWAY_URL, "ANTHROPIC_MODEL": route.model})
    elif route.harness == "codex":
        env.update({"OPENAI_BASE_URL": f"{GATEWAY_URL}/v1", "OPENAI_API_BASE": f"{GATEWAY_URL}/v1",
                    "OPENAI_HOST": GATEWAY_URL, "HARBOR_GATEWAY_CODEX_PROVIDER": "1",
                    "HARBOR_GATEWAY_CODEX_MODEL": route.model,
                    "HARBOR_GATEWAY_CODEX_SERVED_MODEL": route.served_model,
                    "HARBOR_GATEWAY_CODEX_CONTEXT": str(target.context_length)})
    else:  # defensive: callers must not route an unrelated harness through this service
        raise ValueError(f"unsupported compatibility gateway harness: {route.harness}")
    return env


def gateway_process_env(parent: Mapping[str, str]) -> dict[str, str]:
    """Expose this checkout only to Harbor when it must import the custom Codex adapter."""
    root = str(Path(__file__).resolve().parents[2])
    inherited = parent.get("PYTHONPATH", "")
    return {**parent, "PYTHONPATH": os.pathsep.join(item for item in (root, inherited) if item)}


def gateway_metadata(route: GatewayRoute) -> dict[str, str]:
    return {"gateway_revision": route.revision, "gateway_identity": route.identity,
            "gateway_agent": gateway_agent_name(route)}


def codex_gateway_setup_command() -> str:
    """Write the HTTP provider and a truthful local catalog using Codex's own instructions."""
    return '''set -eu
test "${HARBOR_GATEWAY_CODEX_PROVIDER:-}" = 1
test -n "${HARBOR_GATEWAY_CODEX_MODEL:-}"
test -n "${HARBOR_GATEWAY_CODEX_SERVED_MODEL:-}"
case "${HARBOR_GATEWAY_CODEX_CONTEXT:-}" in *[!0-9]*|'') exit 1;; esac
mkdir -p "$CODEX_HOME"
codex debug models --bundled >"$CODEX_HOME/bundled-models.json"
python3 <<'PY'
import json
import os
from pathlib import Path

home = Path(os.environ["CODEX_HOME"])
bundled = json.loads((home / "bundled-models.json").read_text())
models = bundled.get("models")
first = models[0] if isinstance(models, list) and models else None
messages = first.get("model_messages") if isinstance(first, dict) else None
if not isinstance(messages, dict) or not messages.get("instructions_template"):
    raise ValueError("Codex bundled catalog has no reusable instruction template")
context = int(os.environ["HARBOR_GATEWAY_CODEX_CONTEXT"])
if context < 32768 or context > 9_007_199_254_740_991:
    raise ValueError("invalid Codex local-model context")
model = dict(first)
model.update({
    "slug": os.environ["HARBOR_GATEWAY_CODEX_MODEL"],
    "display_name": os.environ["HARBOR_GATEWAY_CODEX_SERVED_MODEL"],
    "description": "Local Qwen model through the Ingot compatibility gateway",
    "default_reasoning_level": None,
    "supported_reasoning_levels": [],
    "shell_type": "default",
    "visibility": "none",
    "supported_in_api": True,
    "priority": 99,
    "additional_speed_tiers": [],
    "service_tiers": [],
    "default_service_tier": None,
    "availability_nux": None,
    "upgrade": None,
    "include_skills_usage_instructions": False,
    "include_plugin_usage_instructions": False,
    "include_apps_usage_instructions": False,
    "supports_reasoning_summary_parameter": False,
    "default_reasoning_summary": "none",
    "support_verbosity": False,
    "default_verbosity": None,
    "apply_patch_tool_type": None,
    "web_search_tool_type": "text",
    "truncation_policy": {"mode": "bytes", "limit": 10000},
    "supports_parallel_tool_calls": False,
    "supports_image_detail_original": False,
    "context_window": context,
    "max_context_window": context,
    "auto_compact_token_limit": None,
    "comp_hash": os.environ["HARBOR_GATEWAY_CODEX_MODEL"],
    "effective_context_window_percent": 95,
    "experimental_supported_tools": [],
    "input_modalities": ["text"],
    "supports_search_tool": False,
    "use_responses_lite": False,
    "auto_review_model_override": None,
    "model_specialty": None,
    "tool_mode": None,
    "multi_agent_version": None,
})
(home / "model-catalog.json").write_text(json.dumps({"models": [model]}, separators=(",", ":")))
PY
cat >>"$CODEX_HOME/config.toml" <<TOML
model_catalog_json = "$CODEX_HOME/model-catalog.json"
model_provider = "harbor_compat"
[model_providers.harbor_compat]
name = "Harbor local compatibility gateway"
base_url = "${OPENAI_BASE_URL}"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
TOML
'''


def gateway_agent_name(route: GatewayRoute) -> str:
    return "ingot.optimize.harbor_codex_gateway:GatewayCodex" if route.harness == "codex" else route.harness


def _callback_source(routes: Sequence[GatewayRoute]) -> str:
    output_limits = {route.model: route.output_limit for route in routes if route.output_limit is not None}
    return '''from litellm.integrations.custom_logger import CustomLogger
from ingot.optimize.harbor_gateway import normalize_role_request

OUTPUT_LIMITS = ''' + repr(output_limits) + '''

class GatewayRoleNormalizer(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        return normalize_role_request(data, call_type, OUTPUT_LIMITS)

gateway_role_normalizer = GatewayRoleNormalizer()
'''


def _gateway_config(routes: Sequence[GatewayRoute]) -> dict[str, Any]:
    return {
        "model_list": [
            {"model_name": route.model,
             "litellm_params": {"model": f"custom_openai/{route.served_model}",
                                "api_base": f"os.environ/{route.upstream_env}", "api_key": "local"}}
            for route in routes
        ],
        "router_settings": {"num_retries": 0, "allowed_fails": 0},
        "litellm_settings": {"callbacks": ["role_normalizer.gateway_role_normalizer"]},
        "general_settings": {"master_key": "local"},
    }


def _fixed_bind_occupied() -> bool:
    """Bound the stale-listener probe; any prior listener invalidates this gateway session."""
    try:
        connection = socket.create_connection((GATEWAY_HOST, GATEWAY_PORT), timeout=0.2)
    except OSError:
        return False
    connection.close()
    return True


class GatewaySession:
    """One LiteLLM process per canary invocation; writes an untracked cleanup receipt on exit."""
    def __init__(self, routes: Sequence[tuple[GatewayRoute, LocalTarget]], runtime_dir: Path, *,
                 litellm_bin: str = _LITELLM_BIN,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen,
                 health_timeout: float = 20.0):
        self.routes = tuple(routes)
        self.runtime_dir = runtime_dir
        self.litellm_bin = litellm_bin
        self.popen = popen
        self.health_timeout = health_timeout
        self.process: subprocess.Popen | None = None

    def _write_files(self) -> Path:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "role_normalizer.py").write_text(
            _callback_source([route for route, _ in self.routes]), encoding="utf-8")
        config = self.runtime_dir / "config.json"
        config.write_text(json.dumps(_gateway_config([route for route, _ in self.routes]), indent=2), encoding="utf-8")
        return config

    def _healthy_models(self) -> bool:
        request = urllib.request.Request(
            f"{GATEWAY_URL}/v1/models", headers={"Authorization": "Bearer local"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=1.0) as response:
                if not 200 <= int(getattr(response, "status", 200)) < 300:
                    return False
                payload = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return False
        expected = {route.model for route, _ in self.routes}
        actual = {str(item.get("id")) for item in payload.get("data", [])
                  if isinstance(item, dict)}
        return expected == actual

    def start(self) -> None:
        if self.process is not None:
            return
        try:
            if not self.routes:
                raise ValueError("compatibility gateway needs at least one route")
            if not Path(self.litellm_bin).is_file():
                raise RuntimeError(
                    "compatibility gateway runtime is missing; create .venv-harbor-gateway from "
                    "ingot/optimize/requirements-harbor-gateway.txt")
            if _fixed_bind_occupied():
                raise RuntimeError("compatibility gateway bind is already occupied")
            config = self._write_files()
            env = gateway_process_env(scrub_provider_env(os.environ))
            for route, target in self.routes:
                env[route.upstream_env] = f"{target.base_url}/v1"
            self.process = self.popen(
                [self.litellm_bin, "--config", str(config), "--host", GATEWAY_HOST,
                 "--port", str(GATEWAY_PORT)],
                cwd=Path.cwd(), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + self.health_timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("compatibility gateway exited before health check")
                try:
                    with urllib.request.urlopen(f"{GATEWAY_URL}/health/liveliness", timeout=1.0) as response:
                        if (200 <= int(getattr(response, "status", 200)) < 300
                                and self._healthy_models() and self.process.poll() is None):
                            return
                except (urllib.error.URLError, TimeoutError, OSError):
                    time.sleep(0.2)
            raise RuntimeError("compatibility gateway did not become healthy")
        except Exception:
            self.close("failed-start")
            raise

    def close(self, reason: str = "completed") -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "cleanup.json").write_text(json.dumps({
            "gateway_revisions": sorted({route.revision for route, _ in self.routes}),
            "routes": [route.identity for route, _ in self.routes],
            "reason": reason,
            "stopped": True,
        }, indent=2), encoding="utf-8")

    def __enter__(self) -> "GatewaySession":
        self.start()
        return self

    def __exit__(self, *unused: object) -> None:
        self.close()
