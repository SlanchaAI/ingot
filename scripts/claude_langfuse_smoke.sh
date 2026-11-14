#!/bin/sh
set -eu

LF_URL=${LANGFUSE_BASE_URL:-http://localhost:3100}
LF_DOCKER_URL=${SMOKE_LANGFUSE_DOCKER_URL:-http://host.docker.internal:3100}
LF_PK=${LANGFUSE_PUBLIC_KEY:-pk-lf-local-demo}
LF_SK=${LANGFUSE_SECRET_KEY:-sk-lf-local-demo}
MARKER="ingot-claude-smoke-$(date +%s)"
export SMOKE_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/ingot-claude-smoke.XXXXXX")
CLAUDE_LOG="$WORKDIR/claude.jsonl"
trap 'rm -rf "$WORKDIR"' EXIT INT TERM

command -v claude >/dev/null 2>&1 || { echo "error: claude is required" >&2; exit 1; }
curl -sSf "$LF_URL/api/public/health" >/dev/null
curl -sS -o /dev/null http://localhost:8000/mcp || [ "$?" -eq 52 ]

(cd "$WORKDIR" && CC_LANGFUSE_DEBUG=1 claude -p --output-format stream-json --verbose \
  --permission-mode bypassPermissions --allowedTools=mcp__ingot__route_and_load \
  "Call ingot.route_and_load exactly once with task '$MARKER', then answer with only: $MARKER") \
  >"$CLAUDE_LOG"

CLAUDE_LOG="$CLAUDE_LOG" MARKER="$MARKER" python3 -c '
import json, os

events = [json.loads(line) for line in open(os.environ["CLAUDE_LOG"]) if line.strip()]
marker = os.environ["MARKER"]
blocks = []
def walk(value):
    if isinstance(value, dict):
        blocks.append(value)
        for child in value.values(): walk(child)
    elif isinstance(value, list):
        for child in value: walk(child)
for event in events: walk(event)
uses = [block for block in blocks
        if block.get("type") == "tool_use" and block.get("name") == "mcp__ingot__route_and_load"]
errors = [block for block in blocks if block.get("type") == "tool_result" and block.get("is_error")]
if len(uses) != 1 or errors:
    raise SystemExit(f"expected one successful Ingot MCP call, got uses={len(uses)} errors={len(errors)}")
if marker not in json.dumps(events):
    raise SystemExit("Claude output did not contain the smoke marker")
print(f"Claude MCP call passed: {marker}")
'

LF_URL="$LF_URL" LF_PK="$LF_PK" LF_SK="$LF_SK" MARKER="$MARKER" python3 -c '
import base64, json, os, time, urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

auth = base64.b64encode((os.environ["LF_PK"] + ":" + os.environ["LF_SK"]).encode()).decode()
url = os.environ["LF_URL"].rstrip("/") + "/api/public/v2/observations"
marker = os.environ["MARKER"]
for _ in range(30):
    params = {"limit": 50, "isRootObservation": "true", "fields": "core,io",
              "fromStartTime": os.environ["SMOKE_STARTED_AT"],
              "toStartTime": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()}
    while True:
        request = urllib.request.Request(url + "?" + urlencode(params),
                                         headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
        if any(marker in json.dumps([obs.get("input"), obs.get("output")])
               for obs in payload["data"]):
            print(f"Claude trace round trip passed: {marker}")
            raise SystemExit(0)
        cursor = (payload.get("meta") or {}).get("cursor")
        if not cursor:
            break
        params["cursor"] = cursor
    time.sleep(2)
raise SystemExit(f"Claude trace containing {marker} was not found")
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
        print(f"Claude trace parser passed: {marker}")
        raise SystemExit(0)
raise SystemExit(f"Claude trace containing {marker} was not parsed")
'
