#!/usr/bin/env bash
# Third-party installation is deliberately fail-closed until skills.lock.json contains reviewed,
# commit-pinned sources with content hashes and license metadata. Never clone a mutable default branch
# directly into the live routing library.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCK="$ROOT/skills.lock.json"

if [ ! -f "$LOCK" ]; then
  echo "[fetch] locked source manifest missing: $LOCK" >&2
  exit 2
fi

echo "[fetch] locked source manifest contains no approved sources; use npx skills or gh skill, then review before indexing." >&2
exit 2
