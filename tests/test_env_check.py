"""The optimizer CLIs preflight OPENROUTER_API_KEY and exit with setup help, not a mid-run 401."""
import pytest

from optimize import require_openrouter_key


def test_missing_key_exits_with_setup_help(monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        require_openrouter_key()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY" in err
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


def test_provider_allowlist_composes_with_zdr(monkeypatch):
    from optimize import ZDR_PROVIDER, client_kwargs, openrouter_extra_body
    monkeypatch.delenv("OPENROUTER_PROVIDERS", raising=False)
    assert openrouter_extra_body() == ZDR_PROVIDER                       # default: ZDR only, no pin
    monkeypatch.setenv("OPENROUTER_PROVIDERS", "fireworks, deepinfra")
    body = openrouter_extra_body()
    assert body["provider"]["only"] == ["fireworks", "deepinfra"]
    assert body["provider"]["zdr"] is True                               # pin never relaxes ZDR
    assert "only" not in ZDR_PROVIDER["provider"]                        # constant not mutated
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
    assert "don't serve 'qwen/x'" in msg and "DeepInfra" in msg and "OPENROUTER_PROVIDERS=groq" in msg


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


def test_preflight_reports_every_conflicting_role(monkeypatch):
    import pytest
    import optimize
    monkeypatch.setenv("OPENROUTER_PROVIDERS", "fireworks")
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.setattr(optimize, "provider_conflict",
                        lambda model, pins: None if "gemini" in model else f"nope for {model}")
    with pytest.raises(SystemExit) as exc:
        optimize.preflight_provider_pins()
    text = str(exc.value)
    assert "MODEL=" in text and "GEPA_MODEL=" in text and "JUDGE_MODEL" not in text


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
