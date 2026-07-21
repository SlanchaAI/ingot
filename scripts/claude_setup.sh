#!/bin/sh
set -eu

MCP_URL=${INGOT_MCP_URL:-http://localhost:8000/mcp}

command -v claude >/dev/null 2>&1 || {
  echo "error: Claude Code is not installed" >&2
  exit 1
}
command -v python3 >/dev/null 2>&1 || {
  echo "error: Python 3.9 or newer is required by the Langfuse plugin" >&2
  exit 1
}

python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' || {
  echo "error: Python 3.9 or newer is required by the Langfuse plugin" >&2
  exit 1
}

if claude mcp get ingot >/dev/null 2>&1; then
  echo "Ingot MCP is already configured; leaving it unchanged."
else
  claude mcp add --scope user --transport http ingot "$MCP_URL"
fi

python3 -m pip install --user 'langfuse>=4.0,<5'
claude plugin marketplace add langfuse/Claude-Observability-Plugin
claude plugin install --scope user langfuse-observability@langfuse-observability

echo "Claude Code setup complete."
echo "Restart Claude Code and enter your Langfuse URL and project keys when the plugin prompts."
echo "For the bundled stack use http://localhost:3100 and the keys from docker-compose.yml."
echo "Ask Claude to call ingot.route_and_load once at the start of each request."
