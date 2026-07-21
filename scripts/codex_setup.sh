#!/bin/sh
set -eu

MCP_URL=${INGOT_MCP_URL:-http://localhost:8000/mcp}
LF_URL=${LANGFUSE_BASE_URL:-http://localhost:3100}
LF_PK=${LANGFUSE_PUBLIC_KEY:-pk-lf-local-demo}
LF_SK=${LANGFUSE_SECRET_KEY:-sk-lf-local-demo}

command -v codex >/dev/null 2>&1 || {
  echo "error: Codex is not installed" >&2
  exit 1
}
command -v node >/dev/null 2>&1 || {
  echo "error: Node.js 22 or newer is required by the Langfuse plugin" >&2
  exit 1
}
command -v python3 >/dev/null 2>&1 || {
  echo "error: Python 3 is required to write the Langfuse configuration" >&2
  exit 1
}

NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
if [ "$NODE_MAJOR" -lt 22 ]; then
  echo "error: Node.js 22 or newer is required by the Langfuse plugin" >&2
  exit 1
fi

if codex mcp get ingot >/dev/null 2>&1; then
  echo "Ingot MCP is already configured; leaving it unchanged."
else
  codex mcp add ingot --url "$MCP_URL"
fi

codex plugin marketplace add langfuse/codex-observability-plugin
codex plugin add tracing@codex-observability-plugin

CODEX_LANGFUSE_DIR=${CODEX_HOME:-$HOME/.codex}
mkdir -p "$CODEX_LANGFUSE_DIR"
umask 077
LANGFUSE_CONFIG="$CODEX_LANGFUSE_DIR/langfuse.json"
LANGFUSE_CONFIG="$LANGFUSE_CONFIG" LF_URL="$LF_URL" LF_PK="$LF_PK" LF_SK="$LF_SK" python3 -c '
import json, os, pathlib
path = pathlib.Path(os.environ["LANGFUSE_CONFIG"])
path.write_text(json.dumps({
    "enabled": True,
    "public_key": os.environ["LF_PK"],
    "secret_key": os.environ["LF_SK"],
    "base_url": os.environ["LF_URL"],
}, indent=2) + "\n")
path.chmod(0o600)
'

echo "Codex setup complete. Restart Codex to load the MCP server and Langfuse plugin."
echo "Credentials were written to $LANGFUSE_CONFIG with user-only permissions."
echo "Ask Codex to call ingot.route_and_load once at the start of each request."
