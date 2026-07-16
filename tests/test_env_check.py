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
