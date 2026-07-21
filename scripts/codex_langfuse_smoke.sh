#!/bin/sh
set -eu

LF_URL=${LANGFUSE_BASE_URL:-http://localhost:3100}
LF_DOCKER_URL=${SMOKE_LANGFUSE_DOCKER_URL:-http://host.docker.internal:3100}
LF_PK=${LANGFUSE_PUBLIC_KEY:-pk-lf-local-demo}
LF_SK=${LANGFUSE_SECRET_KEY:-sk-lf-local-demo}
MARKER="ingot-codex-smoke-$(date +%s)"
WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/ingot-codex-smoke.XXXXXX")
CODEX_LOG="$WORKDIR/codex.log"
trap 'rm -rf "$WORKDIR"' EXIT INT TERM

command -v codex >/dev/null 2>&1 || { echo "error: codex is required" >&2; exit 1; }
curl -sSf "$LF_URL/api/public/health" >/dev/null
curl -sS -o /dev/null http://localhost:8000/mcp || [ "$?" -eq 52 ]

# This opt-in smoke runs in an empty temporary directory and requests only the read-only router.
# Non-interactive Codex otherwise rejects MCP tool approval instead of prompting.
if ! codex exec --cd "$WORKDIR" --skip-git-repo-check \
  --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust \
  "Call ingot.route_and_load exactly once with task '$MARKER', then answer with only: $MARKER" \
  >"$CODEX_LOG" 2>&1; then
  cat "$CODEX_LOG"
  exit 1
fi
cat "$CODEX_LOG"
grep -F "ingot/route_and_load (completed)" "$CODEX_LOG" >/dev/null || {
  echo "error: Codex did not complete the Ingot MCP call" >&2
  exit 1
}

LF_URL="$LF_URL" LF_PK="$LF_PK" LF_SK="$LF_SK" MARKER="$MARKER" python3 -c '
import base64, json, os, time, urllib.request

auth = base64.b64encode((os.environ["LF_PK"] + ":" + os.environ["LF_SK"]).encode()).decode()
url = os.environ["LF_URL"].rstrip("/") + "/api/public/traces?limit=50"
marker = os.environ["MARKER"]
for _ in range(30):
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        traces = json.load(response).get("data", [])
    for trace in traces:
        if marker in json.dumps([trace.get("input"), trace.get("output")]):
            inp, out = trace.get("input"), trace.get("output")
            if not isinstance(inp, str) or not isinstance(out, str):
                raise SystemExit(f"unexpected Codex trace root shape: {type(inp)}, {type(out)}")
            print(f"Codex trace round trip passed: {marker}")
            raise SystemExit(0)
    time.sleep(2)
raise SystemExit(f"Codex trace containing {marker} was not found")
'

docker run --rm --add-host host.docker.internal:host-gateway -v "$PWD:/app" -w /app \
  -e LANGFUSE_BASE_URL="$LF_DOCKER_URL" \
  -e LANGFUSE_PUBLIC_KEY="$LF_PK" \
  -e LANGFUSE_SECRET_KEY="$LF_SK" \
  -e SMOKE_MARKER="$MARKER" ingot-mcp python -c '
import os
from optimize.mine import fetch_traces

marker = os.environ["SMOKE_MARKER"]
for trace in fetch_traces(50):
    if marker in trace["task"] or marker in trace["answer"]:
        assert marker in trace["answer"], trace
        print(f"Codex trace parser passed: {marker}")
        raise SystemExit(0)
raise SystemExit(f"Codex trace containing {marker} was not parsed")
'
