"""Offline script contracts: real shells/files, synthetic CLI and HTTP boundaries."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def shell(tmp_path):
    scripts = tmp_path / 'scripts'
    shutil.copytree(ROOT / 'scripts', scripts)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    calls = tmp_path / 'calls'
    env = {**os.environ, 'PATH': f'{bin_dir}:/usr/bin:/bin', 'TMPDIR': str(tmp_path),
           'CALLS': str(calls), 'HOME': str(tmp_path / 'home')}
    for key in list(env):
        if key.startswith(('LANGFUSE_', 'SMOKE_', 'COMPOSE_SMOKE_')):
            env.pop(key)

    def cli(name, body):
        path = bin_dir / name
        path.write_text('#!/bin/sh\nset -eu\n' + body)
        path.chmod(0o755)

    def run(name, *args):
        return subprocess.run(['bash', '-x', str(scripts / name), *args], cwd=tmp_path,
                              env=env, capture_output=True, text=True, timeout=15)

    return tmp_path, env, cli, run


def test_fetch_copies_bundled_resources_caps_and_never_clobbers(shell):
    root, env, cli, run = shell
    cli('git', '''
for dest; do :; done
mkdir -p "$dest/a" "$dest/b"
printf 'original' > "$dest/a/SKILL.md"
printf 'resource' > "$dest/a/help.txt"
printf 'second' > "$dest/b/SKILL.md"
''')
    result = run('fetch_skills.sh', 'anthropics')
    assert result.returncode == 0, result.stderr
    assert (root / 'skills/a/help.txt').read_text() == 'resource'
    (root / 'skills/a/SKILL.md').write_text('local edits')
    assert run('fetch_skills.sh', 'anthropics').returncode == 0
    assert (root / 'skills/a/SKILL.md').read_text() == 'local edits'
    assert not list(root.glob('tmp.*'))
    assert run('fetch_skills.sh', 'unknown').returncode == 1
    assert 'usage:' in run('fetch_skills.sh').stdout
    cli('git', 'exit 23\n')
    assert run('fetch_skills.sh', 'lambdatest').returncode == 23


def test_compose_smoke_health_success_and_timeout_cleanup(shell):
    root, env, cli, run = shell
    cli('docker', 'echo "$*" >> "$CALLS"\n')
    cli('curl', 'exit "${CURL_FAILURE:-0}"\n')
    cli('sleep', ':\n')
    result = run('compose_smoke.sh')
    assert result.returncode == 0 and 'compose smoke passed' in result.stdout
    calls = (root / 'calls').read_text()
    assert 'up -d --build mcp ui langfuse-web langfuse-worker langfuse-proxy' in calls
    assert 'down -v --remove-orphans' in calls
    env.update(CURL_FAILURE='1', COMPOSE_SMOKE_TIMEOUT='2')
    result = run('compose_smoke.sh')
    assert result.returncode != 0 and 'did not become healthy' in result.stderr
    assert (root / 'calls').read_text().count('down -v --remove-orphans') == 2


@pytest.mark.parametrize('script,client', [
    ('codex_langfuse_smoke.sh', 'codex'), ('claude_langfuse_smoke.sh', 'claude')])
def test_trace_smoke_synthetic_round_trip_and_cli_failure(shell, script, client):
    root, env, cli, run = shell
    cli('curl', ':\n')
    # These are fixture executables, never installed agents or billed model calls.
    cli(client, '''
if [ "${FAIL_CLIENT:-0}" = 1 ]; then echo fixture-failure >&2; exit 9; fi
for prompt; do :; done
marker=$(printf '%s' "$prompt" | sed -n "s/.*task '\\([^']*\\)'.*/\\1/p")
printf '%s' "$marker" > "$MARKER_FILE"
''' + ('''printf 'ingot/route_and_load (completed)\\n%s\\n' "$marker"
''' if client == 'codex' else '''printf '{"type":"tool_use","name":"mcp__ingot__route_and_load","input":"%s"}\\n' "$marker"
'''))
    env['MARKER_FILE'] = str(root / 'marker')
    # Execute the script's embedded Python against a synthetic urllib transport.
    (root / 'sitecustomize.py').write_text('''
import io, json, os, urllib.request

def fixture_urlopen(request, timeout=10):
    assert request.full_url.endswith('/api/public/traces?limit=50')
    assert request.get_header('Authorization').startswith('Basic ')
    marker = open(os.environ['MARKER_FILE']).read()
    return io.BytesIO(json.dumps({'data': [{'input': marker, 'output': marker}]}).encode())
urllib.request.urlopen = fixture_urlopen
''')
    env['PYTHONPATH'] = str(root)
    cli('python3', f'exec "{sys.executable}" "$@"\n')
    cli('docker', 'echo "$*" >> "$CALLS"\n')
    result = run(script)
    assert result.returncode == 0, result.stderr
    assert 'trace round trip passed' in result.stdout
    assert 'optimize.mine' in (root / 'calls').read_text()
    assert not list(root.glob('ingot-*-smoke.*'))
    env['FAIL_CLIENT'] = '1'
    result = run(script)
    assert result.returncode != 0
    assert not list(root.glob('ingot-*-smoke.*'))
