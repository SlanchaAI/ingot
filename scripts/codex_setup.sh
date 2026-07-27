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
# The 0.128 floor stands for one thing: the `codex plugin` subcommand this script
# installs through. Probe for that directly. A locally built codex stamps no
# version — `codex-cli 0.0.0` parses as older than every release while carrying
# the capability, so a version comparison rejects a build that works.
if ! codex plugin --help >/dev/null 2>&1; then
  echo "error: this codex build has no 'plugin' subcommand, which the Langfuse plugin needs" >&2
  echo "       (released builds carry it from 0.128; found version $CODEX_VERSION)" >&2
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
# Codex runs a hook only after a human has reviewed it once and Codex has persisted
# the approval. Until then the Stop hook is skipped in silence: the plugin reports
# installed and enabled, and no trace is ever written.
TRUST_OK=0
grep -q 'tracing@codex-observability-plugin:hooks/hooks.json:stop' \
  "${CODEX_HOME:-$HOME/.codex}/config.toml" 2>/dev/null && TRUST_OK=1

if [ "$MODE" = "doctor" ]; then
  echo "Codex version: $CODEX_VERSION"
  echo "Node version: $(node --version)"
  [ "$MCP_OK" = "1" ] && echo "Ingot MCP: configured at $MCP_URL" || echo "Ingot MCP: missing or URL mismatch"
  [ "$MARKETPLACE_OK" = "1" ] && echo "Langfuse marketplace: installed" || echo "Langfuse marketplace: missing"
  [ "$PLUGIN_OK" = "1" ] && echo "Langfuse plugin: installed" || echo "Langfuse plugin: missing"
  [ -f "$LANGFUSE_CONFIG" ] && echo "Langfuse config: present at $LANGFUSE_CONFIG" || echo "Langfuse config: missing"
  [ "$TRUST_OK" = "1" ] && echo "Stop hook: trusted" || echo "Stop hook: not yet trusted — run codex interactively once and approve it"
  # Probe with Node rather than curl. Node is what uploads the traces, and it reads a
  # CA store of its own: against a private CA, curl can report a healthy endpoint that
  # Node cannot reach. Whichever curl happens to sit first on PATH answers for a third
  # trust store, so it speaks for nothing here.
  if node -e '
const url = process.argv[1] + "/api/public/health";
const lib = url.startsWith("https:") ? require("https") : require("http");
const req = lib.get(url, { timeout: 10000 }, (res) => {
  res.resume();
  process.exit(res.statusCode === 200 ? 0 : 1);
});
req.on("timeout", () => { req.destroy(); process.exit(1); });
req.on("error", () => process.exit(1));
' "$LF_URL" 2>/dev/null; then
    echo "Langfuse endpoint: healthy at $LF_URL"
  else
    echo "Langfuse endpoint: unreachable from Node at $LF_URL"
    echo "  (if curl reaches it, Node is missing the CA — set NODE_EXTRA_CA_CERTS to the root)"
  fi
  trap - 0
  [ "$MCP_OK" = "1" ] && [ "$MARKETPLACE_OK" = "1" ] && [ "$PLUGIN_OK" = "1" ] \
    && [ -f "$LANGFUSE_CONFIG" ] && [ "$TRUST_OK" = "1" ]
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
