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
LF_PK=${LANGFUSE_PUBLIC_KEY:-pk-lf-local-demo}
LF_SK=${LANGFUSE_SECRET_KEY:-sk-lf-local-demo}
trap 'echo "error: Claude setup failed; run $0 --doctor for diagnostics or $0 --repair to reinstall managed state" >&2' 0

command -v claude >/dev/null 2>&1 || { echo "error: Claude Code is not installed" >&2; exit 1; }
UV_OK=0
command -v uv >/dev/null 2>&1 && UV_OK=1
PYTHON_BIN=${INGOT_PYTHON:-python3}
PYTHON_OK=0
SDK_OK=0
if command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1 \
    && PYTHON_OK=1
  "$PYTHON_BIN" -c 'import importlib.metadata as m; v=m.version("langfuse"); raise SystemExit(v.split(".")[0] != "4")' \
    >/dev/null 2>&1 && SDK_OK=1
fi
if [ "$UV_OK" != "1" ] && [ "$PYTHON_OK" != "1" ]; then
  echo "error: install uv (recommended) or Python 3.10 or newer for the Langfuse plugin" >&2
  exit 1
fi

MCP_DETAILS=$(claude mcp get ingot 2>/dev/null || true)
MCP_OK=0
printf '%s' "$MCP_DETAILS" | grep -F "$MCP_URL" >/dev/null 2>&1 && MCP_OK=1
MARKETPLACE_OK=0
claude plugin marketplace list --json 2>/dev/null | grep -F 'langfuse-observability' >/dev/null && MARKETPLACE_OK=1
PLUGIN_OK=0
claude plugin list --json 2>/dev/null | grep -F 'langfuse-observability@langfuse-observability' >/dev/null && PLUGIN_OK=1
if [ "$MODE" = "doctor" ]; then
  echo "Claude Code version: $(claude --version)"
  if [ "$UV_OK" = "1" ]; then
    echo "Langfuse hook runtime: $(uv --version)"
  else
    echo "Python version: $("$PYTHON_BIN" --version)"
  fi
  [ "$MCP_OK" = "1" ] && echo "Ingot MCP: configured at $MCP_URL" || echo "Ingot MCP: missing or URL mismatch"
  [ "$MARKETPLACE_OK" = "1" ] && echo "Langfuse marketplace: installed" || echo "Langfuse marketplace: missing"
  [ "$PLUGIN_OK" = "1" ] && echo "Langfuse plugin: installed" || echo "Langfuse plugin: missing"
  [ "$PLUGIN_OK" = "1" ] && echo "Langfuse plugin configuration: hidden by Claude CLI; --repair rewrites it"
  if [ "$UV_OK" = "1" ]; then
    echo "Langfuse SDK provisioning: managed by uv"
  elif [ "$SDK_OK" = "1" ]; then
    echo "Langfuse Python SDK: compatible 4.x"
  else
    echo "Langfuse Python SDK: missing or incompatible"
  fi
  if command -v curl >/dev/null 2>&1 && curl -sSf "$LF_URL/api/public/health" >/dev/null 2>&1; then
    echo "Langfuse endpoint: healthy at $LF_URL"
  else
    echo "Langfuse endpoint: unreachable at $LF_URL"
  fi
  trap - 0
  [ "$MCP_OK" = "1" ] && [ "$MARKETPLACE_OK" = "1" ] && [ "$PLUGIN_OK" = "1" ] \
    && { [ "$UV_OK" = "1" ] || [ "$SDK_OK" = "1" ]; }
  exit
fi

if [ "$MCP_OK" != "1" ]; then
  if [ -n "$MCP_DETAILS" ]; then
    if [ "$MODE" = "repair" ]; then
      claude mcp remove --scope user ingot
    else
      echo "error: Ingot MCP exists with a different URL; run $0 --repair" >&2
      exit 1
    fi
  fi
  claude mcp add --scope user --transport http ingot "$MCP_URL"
else
  echo "Ingot MCP is already configured at $MCP_URL."
fi

if [ "$UV_OK" = "1" ]; then
  echo "uv is available; the Langfuse plugin will provision its pinned SDK environment."
elif [ "$SDK_OK" != "1" ] || [ "$MODE" = "repair" ]; then
  "$PYTHON_BIN" -m pip install --user --upgrade 'langfuse>=4.0,<5'
else
  echo "Langfuse Python SDK 4.x is already installed."
fi
if [ "$MARKETPLACE_OK" != "1" ]; then
  claude plugin marketplace add langfuse/Claude-Observability-Plugin
fi
if [ "$MODE" = "repair" ] && [ "$PLUGIN_OK" = "1" ]; then
  claude plugin uninstall --scope user --yes langfuse-observability@langfuse-observability
  PLUGIN_OK=0
fi
if [ "$PLUGIN_OK" != "1" ]; then
  claude plugin install --scope user \
    --config "LANGFUSE_BASE_URL=$LF_URL" \
    --config "LANGFUSE_PUBLIC_KEY=$LF_PK" \
    --config "LANGFUSE_SECRET_KEY=$LF_SK" \
    langfuse-observability@langfuse-observability
else
  echo "Langfuse observability plugin is already installed."
fi

trap - 0
echo "Claude Code setup complete."
echo "Restart Claude Code to load the MCP server and Langfuse plugin."
echo "Langfuse was configured from LANGFUSE_* or the bundled local defaults."
echo "Ask Claude to call ingot.route_and_load once at the start of each request."
