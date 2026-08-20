"""Tests for local Harbor target identity, discovery, routing, and isolation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ingot.optimize import harbor_targets as H


FIXTURES = Path(__file__).parent / "fixtures" / "harbor"
TARGETS = {
    "dell-qwen": ("dot-backbone", 163840, FIXTURES / "qwen-models.json"),
    "spark-deepseek": ("deepseek-v4-flash", 1048576, FIXTURES / "deepseek-models.json"),
    "orin-abliterated": ("ablit35b", 65536, FIXTURES / "orin-models.json"),
}
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


class _Response:
    def __init__(self, payload: dict, status: int | None = None):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def _target(alias: str = "dell-qwen", **changes) -> H.LocalTarget:
    model, context, _ = TARGETS[alias]
    values = {
        "alias": alias,
        "display_name": H.TARGETS[alias]["display_name"],
        "base_url": "http://host:8011",
        "served_model": model,
        "context_length": context,
        "protocols": frozenset({"chat", "responses", "messages"}),
        "family": H.TARGETS[alias]["family"],
        "parameter_billions": H.TARGETS[alias]["parameter_billions"],
        "quantization": H.TARGETS[alias]["quantization"],
        "tool_parser": H.TARGETS[alias]["tool_parser"],
    }
    values.update(changes)
    return H.LocalTarget(**values)


def test_fixtures_preserve_served_ids_and_context_lengths():
    for alias, (served_model, context_length, path) in TARGETS.items():
        payload = json.loads(path.read_text())
        model = payload["data"][0]
        assert model["id"] == served_model
        assert model.get("max_model_len", model.get("meta", {}).get("n_ctx")) == context_length
        assert alias in H.TARGETS


def test_parse_target_accepts_only_configured_aliases_and_normalizes_url():
    target = H.parse_target("dell-qwen=http://host:8011/")
    assert target.alias == "dell-qwen"
    assert target.base_url == "http://host:8011"
    assert target.served_model == "dot-backbone"
    with pytest.raises(ValueError, match="unknown local target alias"):
        H.parse_target("unknown=http://host:8011")


@pytest.mark.parametrize("alias", list(TARGETS))
def test_discovery_finds_configured_model_and_context(alias, monkeypatch):
    served_model, context_length, fixture = TARGETS[alias]
    payload = json.loads(fixture.read_text())

    def fake_urlopen(request, timeout):
        assert request.full_url == "http://local.test/v1/models"
        assert timeout == 3.5
        return _Response(payload)

    monkeypatch.setattr(H.urllib.request, "urlopen", fake_urlopen)
    target = H.discover_target(alias, "http://local.test/", timeout=3.5)
    assert target.served_model == served_model
    assert target.context_length == context_length
    assert target.protocols == frozenset({"chat", "responses", "messages"})


def test_ollama_discovery_reads_loaded_runtime_context(monkeypatch):
    seen = []

    def fake_urlopen(request, timeout):
        seen.append((request.full_url, json.loads(request.data) if request.data else None, timeout))
        if request.full_url.endswith("/v1/models"):
            return _Response({"object": "list", "data": [{"id": "qwen3.5:9b"}]})
        return _Response({"models": [
            {"name": "other", "context_length": 8192},
            {"name": "qwen3.5:9b", "context_length": 262144},
        ]})

    monkeypatch.setattr(H.urllib.request, "urlopen", fake_urlopen)
    target = H.discover_target("orin-qwen35-9b", "http://orin.test:11434", timeout=4)

    assert target.served_model == "qwen3.5:9b"
    assert target.context_length == 262144
    assert target.display_name == "Qwen/Qwen3.5-9B (Q4_K_M)"
    assert seen == [
        ("http://orin.test:11434/v1/models", None, 4),
        ("http://orin.test:11434/api/ps", None, 4),
    ]


@pytest.mark.parametrize(("meta", "message"), [
    ({"n_ctx": True}, "no context length"),
    ({"n_ctx": "65536"}, "no context length"),
    ({"n_ctx": 32767}, "below the minimum"),
    ({}, "no context length"),
    ([], "no context length"),
])
def test_discovery_rejects_invalid_llamacpp_context_metadata(meta, message, monkeypatch):
    payload = {"object": "list", "data": [{"id": "ablit35b", "meta": meta}]}
    monkeypatch.setattr(H.urllib.request, "urlopen", lambda *args, **kwargs: _Response(payload))
    with pytest.raises(ValueError, match=message):
        H.discover_target("orin-abliterated", "http://local.test")


def test_discovery_rejects_short_context_and_wrong_served_model(monkeypatch):
    payload = {"object": "list", "data": [{"id": "dot-backbone", "max_model_len": 8192}]}
    monkeypatch.setattr(H.urllib.request, "urlopen", lambda *args, **kwargs: _Response(payload))
    with pytest.raises(ValueError, match="context length"):
        H.discover_target("dell-qwen", "http://local.test")

    payload["data"][0] = {"id": "other", "max_model_len": 32768}
    with pytest.raises(ValueError, match="served model"):
        H.discover_target("dell-qwen", "http://local.test")


def test_fingerprint_changes_with_normalized_url_or_served_model():
    base = _target()
    assert base.fingerprint == "a7f8512ae664"  # pre-scale-metadata identity stays readable
    assert base.fingerprint == _target(base_url="http://host:8011/").fingerprint
    assert base.fingerprint != _target(base_url="http://host:8002").fingerprint
    assert base.fingerprint != _target(served_model="dot-backbone-other").fingerprint
    assert base.fingerprint == _target(family="Qwen-next").fingerprint
    assert base.fingerprint == _target(parameter_billions=99.0).fingerprint
    assert base.fingerprint == _target(quantization="fp8-load").fingerprint
    assert base.fingerprint == _target(tool_parser="qwen3_coder").fingerprint
    assert base.alias in base.job_slug
    assert base.fingerprint in base.job_slug
    assert "http" not in base.job_slug and "host" not in base.job_slug


def test_qwen_size_targets_have_exact_scale_provenance():
    assert {
        alias: {key: config[key] for key in (
            "served_model", "family", "parameter_billions", "quantization", "tool_parser")}
        for alias, config in H.TARGETS.items() if alias.startswith("qwen35-")
    } == {
        "qwen35-08b": {"served_model": "qwen35-0.8b", "family": "Qwen3.5",
                        "parameter_billions": 0.8, "quantization": "fp8-load",
                        "tool_parser": "qwen3_coder"},
        "qwen35-2b": {"served_model": "qwen35-2b", "family": "Qwen3.5",
                       "parameter_billions": 2.0, "quantization": "fp8-load",
                       "tool_parser": "qwen3_coder"},
        "qwen35-4b": {"served_model": "qwen35-4b", "family": "Qwen3.5",
                       "parameter_billions": 4.0, "quantization": "fp8-load",
                       "tool_parser": "qwen3_coder"},
        "qwen35-9b": {"served_model": "qwen35-9b", "family": "Qwen3.5",
                       "parameter_billions": 9.0, "quantization": "fp8-load",
                       "tool_parser": "qwen3_coder"},
    }
    assert {key: H.TARGETS["dell-qwen"][key] for key in (
        "family", "parameter_billions", "quantization", "tool_parser")
    } == {"family": "Qwen3.6", "parameter_billions": 27.0,
          "quantization": "fp8-published", "tool_parser": "qwen3_xml"}
    assert {key: H.TARGETS["orin-qwen35-9b"][key] for key in (
        "served_model", "family", "parameter_billions", "quantization", "tool_parser")
    } == {"served_model": "qwen3.5:9b", "family": "Qwen3.5",
          "parameter_billions": 9.7, "quantization": "Q4_K_M",
          "tool_parser": "ollama"}


@pytest.mark.parametrize("alias", ["qwen35-08b", "qwen35-2b", "qwen35-4b", "qwen35-9b"])
def test_qwen_size_aliases_propagate_provenance_through_parse(alias):
    target = H.parse_target(f"{alias}=http://local.test:8020")
    config = H.TARGETS[alias]
    assert {field: getattr(target, field) for field in (
        "family", "parameter_billions", "quantization", "tool_parser")
    } == {field: config[field] for field in (
        "family", "parameter_billions", "quantization", "tool_parser")}


@pytest.mark.parametrize("harness, protocol", list(HARNESS_PROTOCOLS.items()))
def test_harnesses_map_to_the_required_protocol(harness, protocol):
    assert H.protocol_for(harness) == protocol
    expected = "dot-backbone" if harness == "claude-code" else "openai/dot-backbone"
    if harness == "opencode":
        expected = "local/dot-backbone"
    assert H.harbor_model(_target(), harness) == expected


def test_harbor_kwargs_use_direct_api_base_only_for_terminus():
    target = _target()
    assert H.harbor_agent_kwargs(target, "terminus-2") == {"api_base": f"{target.base_url}/v1"}
    assert H.harbor_agent_kwargs(target, "openclaw") == {"thinking": "off"}
    assert H.harbor_model(target, "opencode") == "local/dot-backbone"
    assert H.harbor_agent_kwargs(target, "opencode") == {
        "opencode_config": {
            "provider": {
                "local": {
                    "npm": "@ai-sdk/openai-compatible",
                    "options": {"baseURL": f"{target.base_url}/v1", "apiKey": "local"},
                    "models": {"dot-backbone": {
                        "limit": {"context": 163840, "output": 40960},
                    }},
                }
            }
        }
    }
    assert H.harbor_agent_kwargs(target, "claude-code") == {}


def test_scrub_provider_env_removes_credentials_and_provider_routing():
    parent = {
        "PATH": "/bin",
        "ANTHROPIC_AUTH_TOKEN": "secret-a",
        "CLAUDE_CODE_OAUTH_TOKEN": "secret-b",
        "CLAUDE_FORCE_OAUTH": "1",
        "ANTHROPIC_BASE_URL": "https://provider.invalid",
        "OPENAI_API_KEY": "secret-c",
        "CODEX_API_KEY": "secret-d",
        "CODEX_FORCE_AUTH_JSON": "1",
        "OPENAI_BASE_URL": "https://provider.invalid",
        "LITELLM_API_KEY": "secret-e",
        "GEMINI_API_KEY": "secret-f",
        "OPENROUTER_API_KEY": "secret-g",
        "API_KEY": "secret-h",
        "MODEL_API_KEY": "secret-i",
    }
    scrubbed = H.scrub_provider_env(parent)
    assert scrubbed == {"PATH": "/bin"}


@pytest.mark.parametrize("harness, key, base, model", [
    ("claude-code", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"),
    ("codex", "OPENAI_API_KEY", "OPENAI_BASE_URL", None),
    ("goose", "OPENAI_API_KEY", "OPENAI_BASE_URL", None),
])
def test_local_environment_uses_sentinel_key_and_explicit_routing(
    harness, key, base, model, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong.invalid")
    monkeypatch.setenv("UNRELATED_AMBIENT_SECRET", "must-not-reach-harbor")
    target = _target()
    env = H.local_agent_env(target, harness)
    assert env[key] == "local"
    assert env["ANTHROPIC_API_KEY"] == "local"
    assert env["OPENAI_API_KEY"] == "local"
    assert env["CODEX_API_KEY"] == "local"
    expected_base = target.base_url if harness == "claude-code" else f"{target.base_url}/v1"
    assert env[base] == expected_base
    if harness != "claude-code":
        assert env["OPENAI_API_BASE"] == expected_base
        assert env["OPENAI_HOST"] == target.base_url
    if model:
        assert env[model] == target.served_model
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "UNRELATED_AMBIENT_SECRET" not in env
    assert env.get("CODEX_API_KEY", "local") == "local"


def test_probe_protocol_posts_one_token_and_requires_nonempty_object(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen.update(url=request.full_url, timeout=timeout, body=json.loads(request.data))
        return _Response({"id": "response-1", "object": "response"})

    monkeypatch.setattr(H.urllib.request, "urlopen", fake_urlopen)
    H.probe_protocol(_target(), "responses", timeout=7.0)
    assert seen["url"] == "http://host:8011/v1/responses"
    assert seen["timeout"] == 7.0
    assert seen["body"]["model"] == "dot-backbone"
    assert seen["body"]["max_output_tokens"] == 1

    monkeypatch.setattr(H.urllib.request, "urlopen", lambda *args, **kwargs: _Response({}))
    with pytest.raises(RuntimeError, match="non-empty"):
        H.probe_protocol(_target(), "chat")


def test_qwen_chat_probe_suppresses_thinking_before_the_one_token_cap(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen.update(body=json.loads(request.data))
        return _Response({"choices": [{"message": {"content": "ready"}}]})

    monkeypatch.setattr(H.urllib.request, "urlopen", fake_urlopen)
    H.probe_protocol(_target(), "chat")

    assert seen["body"]["messages"] == [{"role": "user", "content": "ping /no_think"}]


@pytest.mark.parametrize("status", [199, 300, 302, 399, 400, 500])
def test_probe_protocol_requires_a_2xx_http_status(status, monkeypatch):
    monkeypatch.setattr(
        H.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _Response({"id": "response-1"}, status=status),
    )
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        H.probe_protocol(_target(), "chat")


def test_unsupported_harness_and_protocol_fail_closed():
    target = _target()
    with pytest.raises(ValueError, match="unsupported harness"):
        H.protocol_for("not-a-harness")
    with pytest.raises(ValueError, match="unsupported protocol"):
        H.probe_protocol(target, "xml")
    with pytest.raises(ValueError, match="unsupported harness"):
        H.harbor_agent_kwargs(target, "not-a-harness")


def test_probe_chat_tool_round_trip_disables_thinking_and_returns_tool_result(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        body = json.loads(request.data)
        requests.append((request.full_url, timeout, body))
        if len(requests) == 1:
            return _Response({
                "choices": [{"message": {"role": "assistant", "content": "", "reasoning": "hidden",
                    "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "ingot_echo", "arguments": '{"value":"cutover-ok"}'},
                }]}}],
            })
        return _Response({"choices": [{"message": {"role": "assistant", "content": "cutover-ok"}}]})

    monkeypatch.setattr(H.urllib.request, "urlopen", fake_urlopen)
    H.probe_chat_tool_round_trip(_target(), timeout=9.0)

    assert len(requests) == 2
    assert all(url == "http://host:8011/v1/chat/completions" for url, _, _ in requests)
    assert all(timeout == 9.0 for _, timeout, _ in requests)
    assert all(body["model"] == "dot-backbone" for _, _, body in requests)
    assert all(body["chat_template_kwargs"] == {"enable_thinking": False}
               for _, _, body in requests)
    assert all(body["reasoning_effort"] == "none" for _, _, body in requests)
    assert all(body["messages"][0]["content"].endswith("/no_think")
               for _, _, body in requests)
    assert requests[0][2]["tool_choice"] == "required"
    assert requests[1][2]["messages"][-2] == {
        "role": "assistant", "content": "", "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "ingot_echo", "arguments": '{"value":"cutover-ok"}'},
        }],
    }
    assert requests[1][2]["messages"][-1] == {
        "role": "tool", "tool_call_id": "call-1", "content": "cutover-ok",
    }
    assert requests[1][2]["tools"] == requests[0][2]["tools"]
