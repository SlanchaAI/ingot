"""The optimizer CLIs preflight OPENROUTER_API_KEY and exit with setup help, not a mid-run 401."""
import pytest

from optimize import require_openrouter_key


def test_missing_key_exits_with_setup_help(monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        require_openrouter_key()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "API key" in err and "BASE_URL" in err
    assert ".env.example" in err
    assert "openrouter.ai/keys" in err


def test_blank_key_is_treated_as_missing(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "   ")
    with pytest.raises(SystemExit):
        require_openrouter_key()


def test_set_key_passes(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    require_openrouter_key()  # no exit


def test_fully_local_setup_needs_no_key(monkeypatch):
    from optimize import openrouter_key_missing, require_openrouter_key
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_BASE_URL", "http://localhost:11434/v1")   # e.g. Ollama
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    assert openrouter_key_missing() is False
    require_openrouter_key()  # no exit


def test_local_model_but_openrouter_teacher_still_needs_key(monkeypatch):
    import pytest
    from optimize import require_openrouter_key
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)                 # teacher on OpenRouter
    monkeypatch.setenv("MODEL_BASE_URL", "http://localhost:8000/v1")         # agent on local vLLM
    with pytest.raises(SystemExit):
        require_openrouter_key()


def test_client_kwargs_openrouter_gets_zdr_local_does_not(monkeypatch):
    from optimize import ZDR_PROVIDER, client_kwargs
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    kw = client_kwargs("https://openrouter.ai/api/v1")
    assert kw == {"base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-test",
                  "extra_body": ZDR_PROVIDER}
    kw = client_kwargs("http://localhost:8000/v1")
    assert kw["extra_body"] == {} and kw["api_key"] == "sk-or-test"
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert client_kwargs("http://localhost:8000/v1")["api_key"] == "local"   # client needs SOME key


def test_model_base_url_overrides_only_the_serving_role(monkeypatch):
    from optimize import model_base_url, teacher_base_url
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    assert model_base_url() == teacher_base_url() == "https://openrouter.ai/api/v1"
    monkeypatch.setenv("MODEL_BASE_URL", "http://vllm:8000/v1")
    assert model_base_url() == "http://vllm:8000/v1"
    assert teacher_base_url() == "https://openrouter.ai/api/v1"


def test_provider_priority_composes_with_zdr(monkeypatch):
    from optimize import ZDR_PROVIDER, client_kwargs, openrouter_extra_body
    monkeypatch.delenv("OPENROUTER_PROVIDERS", raising=False)
    assert openrouter_extra_body() == ZDR_PROVIDER                       # default: ZDR only, no pin
    monkeypatch.setenv("OPENROUTER_PROVIDERS", "fireworks, groq")
    body = openrouter_extra_body()
    assert body["provider"]["order"] == ["fireworks", "groq"]            # priority, in given order
    assert body["provider"]["zdr"] is True                               # priority never relaxes ZDR
    assert "order" not in ZDR_PROVIDER["provider"]                       # constant not mutated
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert client_kwargs("https://openrouter.ai/api/v1")["extra_body"] == body
    assert client_kwargs("http://localhost:8000/v1")["extra_body"] == {}  # local: no prefs at all


class _FakeEndpoints:
    def __init__(self, providers):
        import json
        self._body = json.dumps({"data": {"endpoints": [{"provider_name": p} for p in providers]}}).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_provider_conflict_loose_name_matching(monkeypatch):
    import urllib.request
    from optimize import provider_conflict
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=10: _FakeEndpoints(["DeepInfra", "Io Net", "Fireworks"]))
    assert provider_conflict("qwen/x", ["fireworks"]) is None          # display-name vs slug
    assert provider_conflict("qwen/x", ["io-net"]) is None             # punctuation-insensitive
    msg = provider_conflict("qwen/x", ["groq"])
    assert "no provider that serves 'qwen/x'" in msg and "DeepInfra" in msg
    assert "OPENROUTER_PROVIDERS=groq" in msg and "falls back" in msg


def test_provider_conflict_unknown_model_and_network_failure(monkeypatch):
    import urllib.request
    from optimize import provider_conflict
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=10: _FakeEndpoints([]))
    assert "no endpoints on OpenRouter" in provider_conflict("qwen/typo-27b", ["fireworks"])
    def boom(url, timeout=10):
        raise OSError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert provider_conflict("qwen/x", ["fireworks"]) is None          # fail open offline


def test_preflight_no_pins_makes_no_network_calls(monkeypatch):
    import urllib.request
    from optimize import preflight_provider_pins
    monkeypatch.delenv("OPENROUTER_PROVIDERS", raising=False)
    def forbidden(url, timeout=10):
        raise AssertionError("network call without pins")
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    preflight_provider_pins()  # no-op, no network


def test_preflight_warns_on_every_uncovered_role(monkeypatch):
    import optimize
    monkeypatch.setenv("OPENROUTER_PROVIDERS", "fireworks")
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.setattr(optimize, "provider_conflict",
                        lambda model, pins: None if "gemini" in model else f"nope for {model}")
    text = "\n".join(optimize.preflight_provider_pins())   # warns, never exits: roles fall back
    assert "AGENT_MODEL=" in text and "GEPA_MODEL=" in text and "JUDGE_MODEL" not in text


def test_preflight_reports_agent_model_alias_value(monkeypatch):
    # the pin check must validate the model the agent will actually use, whichever alias set it
    import optimize
    monkeypatch.setenv("OPENROUTER_PROVIDERS", "fireworks")
    for var in ("MODEL_BASE_URL", "BASE_URL", "OPENROUTER_BASE_URL", "MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_MODEL", "brand/new-model")
    monkeypatch.setattr(optimize, "provider_conflict",
                        lambda model, pins: f"nope for {model}")
    assert any("AGENT_MODEL=brand/new-model" in w for w in optimize.preflight_provider_pins())


def test_agent_model_resolution(monkeypatch):
    # AGENT_MODEL wins; MODEL is the legacy alias; then the literal default
    from optimize import agent_model
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    assert agent_model() == "qwen/qwen3.6-27b"
    monkeypatch.setenv("MODEL", "legacy/model")
    assert agent_model() == "legacy/model"
    monkeypatch.setenv("AGENT_MODEL", "new/model")
    assert agent_model() == "new/model"


def test_preflight_checks_strong_model_only_when_explicitly_set(monkeypatch):
    import optimize
    monkeypatch.setenv("OPENROUTER_PROVIDERS", "fireworks")
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.setattr(optimize, "provider_conflict",
                        lambda model, pins: f"nope for {model}")
    monkeypatch.delenv("STRONG_MODEL", raising=False)
    text = "\n".join(optimize.preflight_provider_pins())
    assert "STRONG_MODEL" not in text               # default = GEPA_MODEL, already checked
    monkeypatch.setenv("STRONG_MODEL", "z-ai/glm-5.2-max")
    assert any("STRONG_MODEL=z-ai/glm-5.2-max" in w for w in optimize.preflight_provider_pins())


def test_preflight_skips_roles_on_local_endpoints(monkeypatch):
    import optimize
    monkeypatch.setenv("OPENROUTER_PROVIDERS", "fireworks")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "http://localhost:11434/v1")  # fully local
    monkeypatch.setattr(optimize, "provider_conflict",
                        lambda model, pins: f"nope for {model}")
    optimize.preflight_provider_pins()  # nothing talks to OpenRouter -> nothing to check


def test_invoke_retry_fails_fast_on_permanent_config_errors(monkeypatch):
    import pytest
    from optimize import judge as judge_mod
    monkeypatch.setattr(judge_mod.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("slept")))

    class Doomed:
        calls = 0
        def invoke(self, messages):
            Doomed.calls += 1
            raise ValueError("Error 404: No allowed providers are available for the selected model")
    with pytest.raises(SystemExit) as exc:
        judge_mod.invoke_retry(Doomed(), [])
    assert Doomed.calls == 1                                # no retries, no sleeps
    assert "OpenRouter cannot route this request" in str(exc.value)
    monkeypatch.setenv("OPENROUTER_PROVIDERS", "fireworks")
    with pytest.raises(SystemExit) as exc:
        judge_mod.invoke_retry(Doomed(), [])
    assert "OPENROUTER_PROVIDERS=fireworks" in str(exc.value)


def test_invoke_retry_still_retries_transient_errors(monkeypatch):
    from optimize import judge as judge_mod
    monkeypatch.setattr(judge_mod.time, "sleep", lambda s: None)

    class Flaky:
        calls = 0
        def invoke(self, messages):
            Flaky.calls += 1
            if Flaky.calls < 3:
                raise ValueError("502 upstream hiccup")
            return "answer"
    assert judge_mod.invoke_retry(Flaky(), []) == "answer"
    assert Flaky.calls == 3


def test_generic_base_url_and_api_key_win_with_legacy_fallback(monkeypatch):
    from optimize import api_key, model_api_key, teacher_base_url
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-legacy")
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    assert api_key() == "sk-or-legacy"                      # legacy fallback
    monkeypatch.setenv("API_KEY", "fw_generic")
    assert api_key() == "fw_generic"                        # generic wins
    assert model_api_key() == "fw_generic"                  # serving role falls back to shared
    monkeypatch.setenv("MODEL_API_KEY", "fw_model_role")
    assert model_api_key() == "fw_model_role"
    monkeypatch.setenv("BASE_URL", "https://api.fireworks.ai/inference/v1")
    assert teacher_base_url() == "https://api.fireworks.ai/inference/v1"


def test_hosted_https_endpoint_requires_a_key(monkeypatch):
    from optimize import openrouter_key_missing
    for var in ("API_KEY", "OPENROUTER_API_KEY", "MODEL_API_KEY", "MODEL_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BASE_URL", "https://api.fireworks.ai/inference/v1")
    assert openrouter_key_missing() is True                 # hosted endpoint, no key
    monkeypatch.setenv("API_KEY", "fw_x")
    assert openrouter_key_missing() is False


def test_fireworks_direct_gets_clean_openai_request(monkeypatch):
    from optimize import client_kwargs
    monkeypatch.setenv("API_KEY", "fw_x")
    kw = client_kwargs("https://api.fireworks.ai/inference/v1")
    assert kw == {"base_url": "https://api.fireworks.ai/inference/v1", "api_key": "fw_x",
                  "extra_body": {}}                          # no OpenRouter provider prefs
