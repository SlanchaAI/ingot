#!/bin/sh
# Prove the managed stack's one-writer invariant against real containers.
#
#   scripts/managed_smoke.sh
#
# The static checks (tests/test_compose_managed.py, `docker compose config`) verify what the
# tracked configuration declares. This verifies what Docker actually enforces, which is the only
# thing that makes the claim true: every non-publisher service must fail to write the served
# library, and the publisher must succeed.
#
# Run on dellpromax 2026-08-19 (Docker 29.7.2), passing, with the negative control failing as it
# must. The Mac it was written on has no daemon; run it anywhere that does before repeating the
# control-plane claim.
set -eu

PROJECT=${MANAGED_SMOKE_PROJECT:-ingot-managed-smoke}
COMPOSE="docker compose -p $PROJECT"

# Every check below reaches its container through `docker compose exec`, so this stack needs no
# host port. Asking for one anyway makes the run fail on any machine that already serves something
# on 8000 or 8080 -- a shared Docker host, or a box already running Ingot. `0` takes whatever the
# kernel has free.
INGOT_MCP_PORT=${INGOT_MCP_PORT:-0}
INGOT_UI_PORT=${INGOT_UI_PORT:-0}
export INGOT_MCP_PORT INGOT_UI_PORT

cleanup() {
  if [ "${MANAGED_SMOKE_KEEP:-0}" != "1" ]; then
    $COMPOSE down -v --remove-orphans
  fi
}
trap cleanup EXIT INT TERM

# `ui` pulls in `mcp`; the publisher owns the vault. Langfuse is not part of this invariant.
$COMPOSE up -d --build publisher mcp ui

# The publisher initializes the vault on start; wait for the repository rather than racing it.
elapsed=0
until $COMPOSE exec -T publisher git -C /app/vault rev-parse HEAD >/dev/null 2>&1; do
  elapsed=$((elapsed + 2))
  [ "$elapsed" -ge 60 ] && { echo "error: the publisher never initialized /app/vault" >&2; exit 1; }
  sleep 2
done

failed=0

for service in mcp ui; do
  if $COMPOSE exec -T "$service" sh -c 'touch /app/skills/.smoke-write' 2>/dev/null; then
    echo "FAIL: $service can write the served library; it is not read-only" >&2
    failed=1
  else
    echo "ok: $service cannot write /app/skills"
  fi
done

if $COMPOSE exec -T publisher sh -c 'touch /app/vault/.smoke-write && rm /app/vault/.smoke-write'; then
  echo "ok: the publisher can write /app/vault"
else
  echo "FAIL: the publisher cannot write its own vault" >&2
  failed=1
fi

# The served library the other services read is the vault the publisher writes. A stack where they
# are different directories serves stale bytes and reports no drift, because nothing compares them.
vault_head=$($COMPOSE exec -T publisher git -C /app/vault rev-parse HEAD)
served_head=$($COMPOSE exec -T mcp sh -c 'git -C /app/skills rev-parse HEAD' 2>/dev/null || echo "")
if [ "$vault_head" != "$served_head" ]; then
  echo "FAIL: mcp serves $served_head but the publisher's vault is at $vault_head" >&2
  failed=1
else
  echo "ok: mcp serves the publisher's vault at $vault_head"
fi

if $COMPOSE exec -T mcp ingot status; then
  echo "ok: ingot status reports MANAGED inside the stack"
else
  echo "FAIL: ingot status did not report MANAGED inside the stack" >&2
  failed=1
fi

[ "$failed" -eq 0 ] || { echo "managed smoke FAILED" >&2; exit 1; }
echo "managed smoke passed: one writer, and it is the publisher"
