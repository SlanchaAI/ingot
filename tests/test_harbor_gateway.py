"""Tests for the narrow LiteLLM role-compatibility gateway."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ingot.optimize import harbor_gateway as G
from ingot.optimize.harbor_targets import LocalTarget, TARGETS


def _target(alias: str = "dell-qwen") -> LocalTarget:
    context = {"dell-qwen": 163840, "spark-deepseek": 1048576,
               "orin-abliterated": 65536}[alias]
    return LocalTarget(
        alias=alias,
        display_name=TARGETS[alias]["display_name"],
        base_url="http://local.test:8011",
        served_model=TARGETS[alias]["served_model"],
        context_length=context,
        protocols=frozenset({"chat", "messages", "responses"}),
    )


def test_gateway_routes_only_the_three_observed_role_rejections():
    dell = _target("dell-qwen")
    spark = _target("spark-deepseek")
    assert G.gateway_route(dell, "claude-code") is not None
    assert G.gateway_route(spark, "claude-code") is not None
    assert G.gateway_route(dell, "codex") is not None
    assert G.gateway_route(spark, "codex") is None
    orin = _target("orin-abliterated")
    for harness in ("claude-code", "terminus-2", "goose", "opencode", "openclaw",
                    "mini-swe-agent", "codex", "aider", "pi"):
        assert G.gateway_route(orin, harness) is None
    for harness in ("terminus-2", "goose", "opencode", "openclaw", "mini-swe-agent", "aider", "pi"):
        assert G.gateway_route(dell, harness) is None
    unknown = LocalTarget("third-target", "Third", "http://local.test:9000", "third", 32768,
                          frozenset({"chat", "messages", "responses"}))
    assert G.gateway_route(unknown, "claude-code") is None


def test_gateway_identity_is_stable_and_does_not_expose_upstream_address():
    route = G.gateway_route(_target(), "claude-code")
    assert route is not None
    assert route.revision == f"{G.DELL_CLAUDE_OUTPUT_CAP_REVISION}-20480"
    assert route.model.startswith("harbor-compat-")
    assert "local.test" not in route.identity
    assert route.identity == G.gateway_route(_target(), "claude-code").identity
    assert G.gateway_metadata(route)["gateway_agent"] == "claude-code"


def test_dell_claude_gateway_caps_message_output_at_one_eighth_of_context():
    route = G.gateway_route(_target(), "claude-code")
    assert route is not None
    assert route.output_limit == 20480
    request = {
        "model": route.model,
        "max_tokens": 32000,
        "messages": [{"role": "user", "content": "start"}],
    }
    got = G.normalize_role_request(request, "anthropic_messages", {route.model: route.output_limit})
    assert got["max_tokens"] == 20480


def test_dell_claude_gateway_uses_custom_openai_max_tokens_not_responses_output_key():
    route = G.gateway_route(_target(), "claude-code")
    assert route is not None
    request = {
        "model": route.model,
        "max_output_tokens": 32000,
        "messages": [{"role": "user", "content": "start"}],
    }
    got = G.normalize_role_request(request, "anthropic_messages", {route.model: route.output_limit})
    assert got["max_tokens"] == 20480
    assert "max_output_tokens" not in got


def test_dell_claude_gateway_identity_and_model_change_with_its_context_cap():
    full = G.gateway_route(_target(), "claude-code")
    smaller = G.gateway_route(replace(_target(), context_length=16384), "claude-code")
    assert full is not None and smaller is not None
    assert full.output_limit == 20480 and smaller.output_limit == 2048
    assert full.identity != smaller.identity
    assert full.model != smaller.model


def test_gateway_does_not_change_the_passing_spark_claude_output_budget():
    route = G.gateway_route(_target("spark-deepseek"), "claude-code")
    assert route is not None
    assert route.output_limit is None


def test_spark_claude_gateway_strips_the_custom_openai_unsupported_output_key():
    route = G.gateway_route(_target("spark-deepseek"), "claude-code")
    assert route is not None
    got = G.normalize_role_request({
        "model": route.model,
        "max_output_tokens": 32000,
        "messages": [{"role": "user", "content": "start"}],
    }, "anthropic_messages")
    assert "max_output_tokens" not in got
    assert "max_tokens" not in got


def test_gateway_codex_uses_a_non_websocket_custom_provider_config():
    setup = G.codex_gateway_setup_command()
    assert setup.startswith("set -eu\n")
    assert 'mkdir -p "$CODEX_HOME"' in setup
    assert "python3 <<'PY'" in setup
    assert "node <<" not in setup
    assert 'model_provider = "harbor_compat"' in setup
    assert 'wire_api = "responses"' in setup
    assert "supports_websockets = false" in setup
    assert 'base_url = "${OPENAI_BASE_URL}"' in setup


def test_gateway_codex_builds_truthful_catalog_from_runtime_instructions(tmp_path):
    route = G.gateway_route(_target(), "codex")
    assert route is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text("""#!/bin/sh
cat <<'JSON'
{"models":[{"slug":"bundled","display_name":"Bundled","description":null,
"default_reasoning_level":"medium","supported_reasoning_levels":[{"effort":"medium","description":"Medium"}],
"shell_type":"shell_command","visibility":"list","supported_in_api":true,"priority":1,
"availability_nux":null,"upgrade":null,
"model_messages":{"instructions_template":"runtime-owned instructions","instructions_variables":null,
"approvals":null,"collaboration_modes":null,"auto_review":null,"permissions":null,"token_budget":null},
"support_verbosity":true,"default_verbosity":"medium","apply_patch_tool_type":"freeform",
"truncation_policy":{"mode":"tokens","limit":32000},"supports_parallel_tool_calls":true,
"context_window":272000,"max_context_window":272000,"experimental_supported_tools":["custom"]}]}
JSON
""")
    codex.chmod(0o755)
    home = tmp_path / "codex-home"
    completed = subprocess.run(
        ["sh", "-c", G.codex_gateway_setup_command()],
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CODEX_HOME": str(home),
            "OPENAI_BASE_URL": "http://gateway.test/v1",
            "HARBOR_GATEWAY_CODEX_PROVIDER": "1",
            "HARBOR_GATEWAY_CODEX_MODEL": route.model,
            "HARBOR_GATEWAY_CODEX_SERVED_MODEL": "dot-backbone",
            "HARBOR_GATEWAY_CODEX_CONTEXT": "163840",
        },
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    catalog = json.loads((home / "model-catalog.json").read_text())
    assert catalog == {"models": [{
        **catalog["models"][0],
        "slug": route.model,
        "display_name": "dot-backbone",
        "default_reasoning_level": None,
        "supported_reasoning_levels": [],
        "supports_reasoning_summary_parameter": False,
        "support_verbosity": False,
        "default_verbosity": None,
        "apply_patch_tool_type": None,
        "supports_parallel_tool_calls": False,
        "context_window": 163840,
        "max_context_window": 163840,
        "experimental_supported_tools": [],
        "model_messages": {
            "instructions_template": "runtime-owned instructions",
            "instructions_variables": None,
            "approvals": None,
            "collaboration_modes": None,
            "auto_review": None,
            "permissions": None,
            "token_budget": None,
        },
    }]}
    config = (home / "config.toml").read_text()
    assert f'model_catalog_json = "{home}/model-catalog.json"' in config


def test_gateway_codex_env_enables_only_its_custom_provider_setup():
    route = G.gateway_route(_target(), "codex")
    assert route is not None
    assert "codex-http-catalog-v8" in route.revision
    env = G.gateway_agent_env(_target(), route)
    assert env["HARBOR_GATEWAY_CODEX_PROVIDER"] == "1"
    assert env["HARBOR_GATEWAY_CODEX_MODEL"] == route.model
    assert env["HARBOR_GATEWAY_CODEX_SERVED_MODEL"] == "dot-backbone"
    assert env["HARBOR_GATEWAY_CODEX_CONTEXT"] == "163840"


def test_gateway_process_env_precedes_any_existing_import_path_with_the_project_root():
    routed = G.gateway_process_env({"PYTHONPATH": "/existing", "OPENAI_API_KEY": "secret"})
    assert routed["PYTHONPATH"].split(os.pathsep) == [str(Path(G.__file__).resolve().parents[2]), "/existing"]
    assert routed["OPENAI_API_KEY"] == "secret"


def test_gateway_codex_imports_through_the_harbor_runtime_subprocess():
    runtime = os.environ.get("HARBOR_RUNTIME_PYTHON")
    if not runtime:
        pytest.skip("requires the installed Harbor Python runtime")
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [runtime, "-c", "from ingot.optimize.harbor_codex_gateway import GatewayCodex; "
         "assert GatewayCodex.name() == 'codex'"],
        cwd=root, env={**os.environ, "PYTHONPATH": str(root)}, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_gateway_codex_writes_provider_setup_before_the_harbor_adapter(monkeypatch):
    if importlib.util.find_spec("harbor") is None:
        pytest.skip("requires the installed Harbor Python runtime")
    from harbor.agents.installed.codex import Codex
    from ingot.optimize.harbor_codex_gateway import GatewayCodex

    assert not any(flag.kwarg == "reasoning_effort" for flag in GatewayCodex.CLI_FLAGS)
    calls = []

    class ProbeGatewayCodex(GatewayCodex):
        async def exec_as_agent(self, environment, command, **kwargs):
            calls.append(("setup", command, kwargs))

    async def fake_parent_run(self, instruction, environment, context):
        calls.append(("parent", instruction))

    monkeypatch.setattr(Codex, "run", fake_parent_run)
    asyncio.run(object.__new__(ProbeGatewayCodex).run("task", object(), object()))
    assert calls[0][0] == "setup" and "supports_websockets = false" in calls[0][1]
    assert calls[0][2]["env"]["CODEX_HOME"] == "/tmp/codex-home"
    assert calls[1] == ("parent", "task")


def test_normalizer_moves_anthropic_system_content_to_a_user_message_without_touching_tools():
    tool_use = {"type": "tool_use", "id": "call_1", "name": "shell", "input": {"cmd": "pwd"}}
    tool_result = {"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}
    request = {
        "system": "follow the repository instructions",
        "messages": [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": [tool_use]},
            {"role": "user", "content": [tool_result]},
        ],
    }
    got = G.normalize_role_request(request, "anthropic_messages")
    assert "system" not in got
    assert got["messages"][0] == {
        "role": "user", "content": "[system]\nfollow the repository instructions",
    }
    assert got["messages"][2]["content"] == [tool_use]
    assert got["messages"][3]["content"] == [tool_result]


def test_normalizer_moves_responses_instructions_and_developer_roles_without_touching_tools():
    function_call = {"type": "function_call", "call_id": "call_1", "name": "shell", "arguments": "{}"}
    function_output = {"type": "function_call_output", "call_id": "call_1", "output": "ok"}
    request = {
        "instructions": "follow the repository instructions",
        "input": [
            {"role": "developer", "content": [{"type": "input_text", "text": "be concise"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "start"}]},
            function_call,
            function_output,
        ],
    }
    got = G.normalize_role_request(request, "aresponses")
    assert "instructions" not in got
    assert got["input"][0] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "[instructions]\nfollow the repository instructions"}],
    }
    assert got["input"][1]["role"] == "user"
    assert got["input"][3] == function_call
    assert got["input"][4] == function_output


def test_normalizer_strips_codex_reasoning_from_custom_openai_responses():
    request = {
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "start"}]}],
        "reasoning": {"effort": "high", "summary": "auto"},
        "reasoning_effort": "high",
    }

    got = G.normalize_role_request(request, "aresponses")

    assert "reasoning" not in got
    assert "reasoning_effort" not in got
    assert got["input"] == request["input"]


def test_normalizer_leaves_unrelated_routes_unchanged():
    request = {"messages": [{"role": "system", "content": "unchanged"}]}
    assert G.normalize_role_request(request, "completion") == request


def test_gateway_session_requires_its_revisioned_models_and_writes_cleanup_receipt(tmp_path, monkeypatch):
    route = G.gateway_route(_target(), "claude-code")
    assert route is not None

    class Process:
        def __init__(self):
            self.stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout):
            return 0

    process = Process()
    calls = []
    spawned = {}

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout):
        calls.append(request)
        if isinstance(request, str):
            return Response({"status": "healthy"})
        return Response({"data": [{"id": route.model}]})

    def popen(*args, **kwargs):
        spawned.update(kwargs)
        return process

    monkeypatch.setattr(G.urllib.request, "urlopen", fake_urlopen)
    session = G.GatewaySession([(route, _target())], tmp_path / "gateway",
                               litellm_bin=__file__, popen=popen)
    session.start()
    assert spawned["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(
        Path(G.__file__).resolve().parents[2])
    config = (tmp_path / "gateway" / "config.json").read_text()
    assert "local.test" not in config and route.upstream_env in config
    assert f"custom_openai/{route.served_model}" in config
    assert any(isinstance(call, str) and call.endswith("/health/liveliness") for call in calls)
    session.close()
    receipt = json.loads((tmp_path / "gateway" / "cleanup.json").read_text())
    assert receipt["stopped"] is True and receipt["routes"] == [route.identity]


def test_gateway_failed_start_still_writes_a_cleanup_receipt(tmp_path):
    route = G.gateway_route(_target(), "claude-code")
    assert route is not None

    def fail_start(*args, **kwargs):
        raise OSError("cannot bind")

    session = G.GatewaySession([(route, _target())], tmp_path / "gateway",
                               litellm_bin=__file__, popen=fail_start)
    with pytest.raises(OSError, match="cannot bind"):
        session.start()
    receipt = json.loads((tmp_path / "gateway" / "cleanup.json").read_text())
    assert receipt["reason"] == "failed-start" and receipt["stopped"] is True


def test_gateway_missing_dedicated_runtime_fails_before_start_and_writes_receipt(tmp_path):
    route = G.gateway_route(_target(), "claude-code")
    assert route is not None
    session = G.GatewaySession([(route, _target())], tmp_path / "gateway",
                               litellm_bin=str(tmp_path / "missing-litellm"))
    with pytest.raises(RuntimeError, match="runtime is missing"):
        session.start()
    receipt = json.loads((tmp_path / "gateway" / "cleanup.json").read_text())
    assert receipt["reason"] == "failed-start"


def test_default_gateway_runtime_is_checkout_relative():
    runtime = Path(G._LITELLM_BIN)

    assert runtime.is_absolute()
    assert runtime == Path(G.__file__).resolve().parents[2] / ".venv-harbor-gateway/bin/litellm"


def test_gateway_fixed_bind_probe_is_bounded_and_closes_a_preexisting_listener(monkeypatch):
    class Connection:
        closed = False

        def close(self):
            self.closed = True

    connection = Connection()
    calls = []

    def connect(address, timeout):
        calls.append((address, timeout))
        return connection

    monkeypatch.setattr(G.socket, "create_connection", connect)
    assert G._fixed_bind_occupied() is True
    assert connection.closed is True
    assert calls == [((G.GATEWAY_HOST, G.GATEWAY_PORT), 0.2)]


def test_gateway_rejects_any_preexisting_fixed_bind_before_spawn(tmp_path, monkeypatch):
    route = G.gateway_route(_target(), "claude-code")
    assert route is not None
    monkeypatch.setattr(G, "_fixed_bind_occupied", lambda: True, raising=False)

    def unexpected_spawn(*args, **kwargs):
        pytest.fail("must not spawn beside a stale gateway")

    session = G.GatewaySession([(route, _target())], tmp_path / "gateway",
                               litellm_bin=__file__, popen=unexpected_spawn)
    with pytest.raises(RuntimeError, match="bind is already occupied"):
        session.start()
    receipt = json.loads((tmp_path / "gateway" / "cleanup.json").read_text())
    assert receipt["reason"] == "failed-start" and receipt["stopped"] is True


def test_gateway_rejects_a_post_spawn_model_superset_and_cleans_up(tmp_path, monkeypatch):
    route = G.gateway_route(_target(), "claude-code")
    assert route is not None

    class Process:
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout):
            return 0

    process = Process()
    monkeypatch.setattr(G, "_fixed_bind_occupied", lambda: False, raising=False)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"data": [{"id": route.model}, {"id": "stale-old-route"}]}).encode()

    def fake_urlopen(request, timeout):
        return Response()

    monkeypatch.setattr(G.urllib.request, "urlopen", fake_urlopen)
    session = G.GatewaySession([(route, _target())], tmp_path / "gateway",
                               litellm_bin=__file__, popen=lambda *args, **kwargs: process,
                               health_timeout=0.01)
    with pytest.raises(RuntimeError, match="did not become healthy"):
        session.start()
    assert process.stopped is True
    receipt = json.loads((tmp_path / "gateway" / "cleanup.json").read_text())
    assert receipt["stopped"] is True


def test_gateway_requires_its_spawned_process_alive_after_exact_model_health(tmp_path, monkeypatch):
    route = G.gateway_route(_target(), "claude-code")
    assert route is not None

    class Process:
        polls = [None, 0]
        stopped = False

        def poll(self):
            return self.polls.pop(0) if self.polls else 0

        def terminate(self):
            self.stopped = True

        def wait(self, timeout):
            return 0

    process = Process()
    monkeypatch.setattr(G, "_fixed_bind_occupied", lambda: False, raising=False)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"data": [{"id": route.model}]}).encode()

    monkeypatch.setattr(G.urllib.request, "urlopen", lambda request, timeout: Response())
    session = G.GatewaySession([(route, _target())], tmp_path / "gateway",
                               litellm_bin=__file__, popen=lambda *args, **kwargs: process,
                               health_timeout=0.01)
    with pytest.raises(RuntimeError, match="exited before health check"):
        session.start()
    assert process.stopped is False
    receipt = json.loads((tmp_path / "gateway" / "cleanup.json").read_text())
    assert receipt["stopped"] is True
