"""Run the scripts' actual polling code and mining parser over isolated HTTP."""
import base64
import json
import os
import shlex
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from optimize import mine


@pytest.mark.parametrize("agent,structured,delayed", [
    ("codex", False, False),
    ("codex", False, True),
    ("claude", True, False),
    ("codex", True, False),  # JSON objects must not pass Codex's string-root check.
])
def test_smoke_poll_and_mining_use_v2_roots(monkeypatch, agent, structured, delayed):
    script = (Path(__file__).resolve().parents[1] / "scripts" /
              f"{agent}_langfuse_smoke.sh").read_text()
    tokens = shlex.split(script)
    poll = next(tokens[i + 2] for i, token in enumerate(tokens[:-2])
                if token == "python3" and tokens[i + 1] == "-c"
                and "urllib.request" in tokens[i + 2])
    marker = "isolated-v2-smoke-marker"
    inp, out = marker + " task", marker + " answer"
    if structured:
        inp, out = {"role": "user", "content": inp}, {"role": "assistant", "content": out}
    root = {"id": "root", "traceId": "trace", "input": json.dumps(inp),
            "output": json.dumps(out), "tags": ["smoke"]}
    started = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    requests = []
    expected_auth = "Basic " + base64.b64encode(b"fixture-public:fixture-secret").decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            requests.append((url.path, query, self.headers.get("Authorization")))
            if url.path != "/api/public/v2/observations":
                self.send_error(404)
                return
            cursor = query.get("cursor", [None])[0]
            if delayed and len(requests) == 1:
                payload = {"data": [], "meta": {}}
            elif cursor is None:
                # V2 can return an empty page with a continuation cursor.
                payload = {"data": [], "meta": {"cursor": "next+/="}}
            elif cursor == "next+/=":
                payload = {"data": [{"id": "other", "traceId": "other",
                                     "input": "unrelated", "output": None}],
                           "meta": {"cursor": "last"}}
            else:
                payload = {"data": [root], "meta": {}}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        result = subprocess.run(
            [sys.executable, "-c", poll], capture_output=True, text=True, timeout=15,
            env={**os.environ, "LF_URL": url, "LF_PK": "fixture-public",
                 "LF_SK": "fixture-secret", "MARKER": marker, "SMOKE_STARTED_AT": started})
        if agent == "codex" and structured:
            assert result.returncode != 0
            assert "unexpected Codex trace root shape" in result.stderr
        else:
            assert result.returncode == 0, result.stderr
            assert f"{agent.title()} trace round trip passed: {marker}" in result.stdout
        polling_requests = list(requests)
        monkeypatch.setattr(mine, "LF_URL", url)
        monkeypatch.setattr(mine, "LF_PK", "fixture-public")
        monkeypatch.setattr(mine, "LF_SK", "fixture-secret")
        assert mine.fetch_traces(1) == [{"task": marker + " task", "rubric": "",
                                        "answer": marker + " answer", "tags": ["smoke"]}]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    for path, query, auth in requests:
        assert path == "/api/public/v2/observations"
        assert query["isRootObservation"] == ["true"]
        assert {"core", "io"} <= set(query["fields"][0].split(","))
        assert "page" not in query
        assert auth == expected_auth
        lower = datetime.fromisoformat(query["fromStartTime"][0].replace("Z", "+00:00"))
        upper = datetime.fromisoformat(query["toStartTime"][0].replace("Z", "+00:00"))
        assert lower < upper
    assert [query.get("cursor", [None])[0] for _, query, _ in polling_requests] == (
        [None, None, "next+/=", "last"] if delayed else [None, "next+/=", "last"])
    assert all(query["fromStartTime"] == [started] for _, query, _ in polling_requests)
    if delayed:
        assert polling_requests[0][1]["toStartTime"] < polling_requests[1][1]["toStartTime"]
