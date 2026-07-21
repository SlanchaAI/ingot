"""User-level Claude and Codex setup scripts, exercised with isolated fake CLIs."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o755)


def _environment(tmp_path: Path) -> tuple[dict, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    env = {**os.environ, "HOME": str(tmp_path / "home"), "TEST_STATE": str(state),
           "PATH": f"{fake_bin}:{os.environ['PATH']}", "INGOT_PYTHON": "python3"}
    for name in ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        env.pop(name, None)
    _executable(fake_bin / "python3", f'''
if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "pip" ]; then
  echo "python3 $*" >> "$TEST_STATE/calls"
  exit 0
fi
exec "{sys.executable}" "$@"
''')
    return env, fake_bin


def _run_twice(script: str, env: dict) -> None:
    for _ in range(2):
        subprocess.run([str(ROOT / "scripts" / script)], cwd=ROOT, env=env,
                       text=True, capture_output=True, check=True)


def test_codex_setup_is_idempotent_and_writes_private_config(tmp_path):
    env, fake_bin = _environment(tmp_path)
    _executable(fake_bin / "node", 'echo 22\n')
    _executable(fake_bin / "codex", '''
echo "codex $*" >> "$TEST_STATE/calls"
if [ "$1" = "--version" ]; then echo "codex-cli 0.144.5"; exit 0; fi
if [ "$1 $2 $3" = "mcp get ingot" ]; then
  test -f "$TEST_STATE/mcp" && echo "url: http://localhost:8000/mcp"
  test -f "$TEST_STATE/mcp"
  exit
fi
if [ "$1 $2 $3" = "mcp add ingot" ]; then touch "$TEST_STATE/mcp"; exit; fi
if [ "$1 $2 $3" = "plugin marketplace list" ]; then
  test -f "$TEST_STATE/market" && echo '"codex-observability-plugin"'
  exit
fi
if [ "$1 $2" = "plugin list" ]; then
  test -f "$TEST_STATE/plugin" && echo '"tracing@codex-observability-plugin"'
  exit
fi
if [ "$1 $2 $3" = "plugin marketplace add" ]; then touch "$TEST_STATE/market"; exit; fi
if [ "$1 $2" = "plugin add" ]; then touch "$TEST_STATE/plugin"; exit; fi
''')
    env.update({"LANGFUSE_BASE_URL": "https://langfuse.example",
                "LANGFUSE_PUBLIC_KEY": "pk-test", "LANGFUSE_SECRET_KEY": "sk-test"})

    _run_twice("codex_setup.sh", env)

    calls = (Path(env["TEST_STATE"]) / "calls").read_text()
    assert calls.count("codex mcp add ingot") == 1
    assert calls.count("codex plugin marketplace add") == 1
    assert calls.count("codex plugin add") == 1
    config = Path(env["HOME"]) / ".codex" / "langfuse.json"
    assert json.loads(config.read_text()) == {
        "enabled": True, "public_key": "pk-test", "secret_key": "sk-test",
        "base_url": "https://langfuse.example"}
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_codex_setup_rejects_old_codex_before_writing_config(tmp_path):
    env, fake_bin = _environment(tmp_path)
    _executable(fake_bin / "node", 'echo 22\n')
    _executable(fake_bin / "codex", 'echo "codex-cli 0.127.9"\n')

    result = subprocess.run([str(ROOT / "scripts" / "codex_setup.sh")], cwd=ROOT, env=env,
                            text=True, capture_output=True)

    assert result.returncode != 0
    assert "Codex 0.128 or newer" in result.stderr
    assert not (Path(env["HOME"]) / ".codex" / "langfuse.json").exists()


def test_remote_setup_requires_explicit_langfuse_credentials(tmp_path):
    env, _ = _environment(tmp_path)
    env["LANGFUSE_BASE_URL"] = "https://langfuse.example"

    for script in ("claude_setup.sh", "codex_setup.sh"):
        result = subprocess.run([str(ROOT / "scripts" / script)], cwd=ROOT, env=env,
                                text=True, capture_output=True)
        assert result.returncode != 0
        assert "requires LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY" in result.stderr


def test_claude_setup_is_idempotent(tmp_path):
    env, fake_bin = _environment(tmp_path)
    _executable(fake_bin / "claude", '''
echo "claude $*" >> "$TEST_STATE/calls"
if [ "$1" = "--version" ]; then echo "2.1.206"; exit 0; fi
if [ "$1 $2 $3" = "mcp get ingot" ]; then
  test -f "$TEST_STATE/mcp" && echo "url: http://localhost:8000/mcp"
  test -f "$TEST_STATE/mcp"
  exit
fi
if [ "$1 $2 $3" = "mcp add --scope" ]; then touch "$TEST_STATE/mcp"; exit; fi
if [ "$1 $2 $3" = "plugin marketplace list" ]; then
  test -f "$TEST_STATE/market" && echo '"langfuse-observability"'
  exit
fi
if [ "$1 $2" = "plugin list" ]; then
  test -f "$TEST_STATE/plugin" && echo '"langfuse-observability@langfuse-observability"'
  exit
fi
if [ "$1 $2 $3" = "plugin marketplace add" ]; then touch "$TEST_STATE/market"; exit; fi
if [ "$1 $2" = "plugin install" ]; then touch "$TEST_STATE/plugin"; exit; fi
''')

    _run_twice("claude_setup.sh", env)

    calls = (Path(env["TEST_STATE"]) / "calls").read_text()
    assert calls.count("claude mcp add") == 1
    assert calls.count("plugin marketplace add") == 1
    assert calls.count("plugin install") == 1


def test_codex_doctor_reports_missing_state(tmp_path):
    env, fake_bin = _environment(tmp_path)
    _executable(fake_bin / "node", 'echo 22\n')
    _executable(fake_bin / "codex", '''
if [ "$1" = "--version" ]; then echo "codex-cli 0.144.5"; fi
''')

    result = subprocess.run([str(ROOT / "scripts" / "codex_setup.sh"), "--doctor"],
                            cwd=ROOT, env=env, text=True, capture_output=True)

    assert result.returncode != 0
    assert "Ingot MCP: missing" in result.stdout
    assert "Langfuse plugin: missing" in result.stdout
    assert "Langfuse config: missing" in result.stdout


def test_claude_repair_replaces_mismatched_mcp_and_plugin(tmp_path):
    env, fake_bin = _environment(tmp_path)
    state = Path(env["TEST_STATE"])
    (state / "mcp").touch()
    (state / "market").touch()
    (state / "plugin").touch()
    _executable(fake_bin / "claude", '''
echo "claude $*" >> "$TEST_STATE/calls"
if [ "$1" = "--version" ]; then echo "2.1.206"; exit 0; fi
if [ "$1 $2 $3" = "mcp get ingot" ]; then echo "url: http://old.example/mcp"; exit; fi
if [ "$1 $2 $3" = "mcp remove --scope" ]; then rm -f "$TEST_STATE/mcp"; exit; fi
if [ "$1 $2 $3" = "mcp add --scope" ]; then touch "$TEST_STATE/mcp"; exit; fi
if [ "$1 $2 $3" = "plugin marketplace list" ]; then echo '"langfuse-observability"'; exit; fi
if [ "$1 $2" = "plugin list" ]; then echo '"langfuse-observability@langfuse-observability"'; exit; fi
if [ "$1 $2" = "plugin uninstall" ]; then rm -f "$TEST_STATE/plugin"; exit; fi
if [ "$1 $2" = "plugin install" ]; then touch "$TEST_STATE/plugin"; exit; fi
''')

    subprocess.run([str(ROOT / "scripts" / "claude_setup.sh"), "--repair"], cwd=ROOT,
                   env=env, text=True, capture_output=True, check=True)

    calls = (state / "calls").read_text()
    assert "claude mcp remove --scope user ingot" in calls
    assert "claude mcp add --scope user --transport http ingot" in calls
    assert "claude plugin uninstall" in calls
    assert "claude plugin install" in calls


def test_setup_docs_require_persistent_skill_loading_instruction():
    integration = (ROOT / "docs" / "mcp-integration.md").read_text()
    production = (ROOT / "PRODUCTION_SETUP.md").read_text()

    assert "registration exposes the tool but does not force" in integration.lower()
    assert "CLAUDE.md" in integration and "AGENTS.md" in integration
    assert "call ingot.route_and_load exactly once" in integration
    assert "CLAUDE.md" in production and "AGENTS.md" in production


def test_live_smokes_require_completed_mcp_call_and_mining_parser():
    codex = (ROOT / "scripts" / "codex_langfuse_smoke.sh").read_text()
    claude = (ROOT / "scripts" / "claude_langfuse_smoke.sh").read_text()

    assert "ingot/route_and_load (completed)" in codex
    assert "mcp__ingot__route_and_load" in claude and "tool_result" in claude
    assert "from optimize.mine import fetch_traces" in codex
    assert "from optimize.mine import fetch_traces" in claude
    assert "--add-host host.docker.internal:host-gateway" in codex
    assert "--add-host host.docker.internal:host-gateway" in claude
