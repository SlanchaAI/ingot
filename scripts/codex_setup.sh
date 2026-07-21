#!/bin/sh
set -eu

MODE=setup
case "${1:-}" in
  "") ;;
  --doctor) MODE=doctor ;;
  --repair) MODE=repair ;;
  *) echo "usage: $0 [--doctor|--repair]" >&2; exit 2 ;;
esac

MCP_URL=${INGOT_MCP_URL:-http://localhost:8000/mcp}
LF_URL=${LANGFUSE_BASE_URL:-http://localhost:3100}
case "$LF_URL" in
  http://localhost:3100|http://127.0.0.1:3100) ;;
  *)
    if [ -z "${LANGFUSE_PUBLIC_KEY:-}" ] || [ -z "${LANGFUSE_SECRET_KEY:-}" ]; then
      echo "error: remote LANGFUSE_BASE_URL requires LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY" >&2
      exit 1
    fi
    ;;
esac
LF_PK=${LANGFUSE_PUBLIC_KEY:-pk-lf-local-demo}
LF_SK=${LANGFUSE_SECRET_KEY:-sk-lf-local-demo}
CODEX_LANGFUSE_DIR=${CODEX_HOME:-$HOME/.codex}
LANGFUSE_CONFIG="$CODEX_LANGFUSE_DIR/langfuse.json"

trap 'echo "error: Codex setup failed; run $0 --doctor for diagnostics or $0 --repair to reinstall managed state" >&2' 0

require_command() {
  command -v "$1" >/dev/null 2>&1 || { echo "error: $1 is required" >&2; exit 1; }
}

require_command codex
require_command node
require_command python3

CODEX_VERSION=$(codex --version | awk '{print $2}')
CODEX_MINOR=$(printf '%s' "$CODEX_VERSION" | awk -F. '{print $2}')
if [ "${CODEX_VERSION%%.*}" = "0" ] && [ "${CODEX_MINOR:-0}" -lt 128 ]; then
  echo "error: Codex 0.128 or newer is required by the Langfuse plugin" >&2
  exit 1
fi
NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
if [ "$NODE_MAJOR" -lt 22 ]; then
  echo "error: Node.js 22 or newer is required by the Langfuse plugin" >&2
  exit 1
fi

MCP_DETAILS=$(codex mcp get ingot 2>/dev/null || true)
MCP_OK=0
printf '%s' "$MCP_DETAILS" | grep -F "$MCP_URL" >/dev/null 2>&1 && MCP_OK=1
MARKETPLACE_OK=0
codex plugin marketplace list --json 2>/dev/null | grep -F 'codex-observability-plugin' >/dev/null && MARKETPLACE_OK=1
PLUGIN_OK=0
codex plugin list --json 2>/dev/null | grep -F 'tracing@codex-observability-plugin' >/dev/null && PLUGIN_OK=1

if [ "$MODE" = "doctor" ]; then
  echo "Codex version: $CODEX_VERSION"
  echo "Node version: $(node --version)"
  [ "$MCP_OK" = "1" ] && echo "Ingot MCP: configured at $MCP_URL" || echo "Ingot MCP: missing or URL mismatch"
  [ "$MARKETPLACE_OK" = "1" ] && echo "Langfuse marketplace: installed" || echo "Langfuse marketplace: missing"
  [ "$PLUGIN_OK" = "1" ] && echo "Langfuse plugin: installed" || echo "Langfuse plugin: missing"
  [ -f "$LANGFUSE_CONFIG" ] && echo "Langfuse config: present at $LANGFUSE_CONFIG" || echo "Langfuse config: missing"
  if command -v curl >/dev/null 2>&1 && curl -sSf "$LF_URL/api/public/health" >/dev/null 2>&1; then
    echo "Langfuse endpoint: healthy at $LF_URL"
  else
    echo "Langfuse endpoint: unreachable at $LF_URL"
  fi
  trap - 0
  [ "$MCP_OK" = "1" ] && [ "$MARKETPLACE_OK" = "1" ] && [ "$PLUGIN_OK" = "1" ] && [ -f "$LANGFUSE_CONFIG" ]
  exit
fi

if [ "$MCP_OK" != "1" ]; then
  if [ -n "$MCP_DETAILS" ]; then
    if [ "$MODE" = "repair" ]; then
      codex mcp remove ingot
    else
      echo "error: Ingot MCP exists with a different URL; run $0 --repair" >&2
      exit 1
    fi
  fi
  codex mcp add ingot --url "$MCP_URL"
else
  echo "Ingot MCP is already configured at $MCP_URL."
fi

if [ "$MARKETPLACE_OK" != "1" ]; then
  codex plugin marketplace add langfuse/codex-observability-plugin
fi
if [ "$MODE" = "repair" ] && [ "$PLUGIN_OK" = "1" ]; then
  codex plugin remove tracing@codex-observability-plugin
  PLUGIN_OK=0
fi
if [ "$PLUGIN_OK" != "1" ]; then
  codex plugin add tracing@codex-observability-plugin
else
  echo "Langfuse tracing plugin is already installed."
fi

mkdir -p "$CODEX_LANGFUSE_DIR"
umask 077
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

trap - 0
echo "Codex setup complete. Restart Codex to load the MCP server and Langfuse plugin."
echo "Credentials were written to $LANGFUSE_CONFIG with user-only permissions."
echo "Ask Codex to call ingot.route_and_load once at the start of each request."
