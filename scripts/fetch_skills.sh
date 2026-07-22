#!/usr/bin/env bash
# Fetch example Agent Skills (SKILL.md format) into ./skills/. Every source is OPTIONAL and nothing
# is redistributed in this repo, each source is cloned from upstream, its skills copied in, and the
# clone deleted, so skills stay under their own upstream licenses.
#
# Usage:
#   scripts/fetch_skills.sh all                       # every source below
#   scripts/fetch_skills.sh anthropics lambdatest     # just these
#
# Sources (curated from https://github.com/VoltAgent/awesome-agent-skills):
#   anthropics   anthropics/skills        document skills (pdf, docx, …)      per-skill license (frontmatter)
#   lambdatest   LambdaTest/agent-skills  testing frameworks                  MIT
# Disabled sources (uncomment in SOURCES and lookup() to re-enable):
#   nvidia       nvidia/skills            GPU / infra / data / imaging        Apache-2.0
#   trailofbits  trailofbits/skills       security analysis                   CC-BY-SA-4.0
set -euo pipefail

SKILLS="$(cd "$(dirname "$0")/.." && pwd)/skills"
mkdir -p "$SKILLS"

# copy up to $cap skill dirs (0 = no cap) from a freshly-cloned repo, skipping any that already exist
fetch() {  # repo  cap  license
  local repo="$1" cap="$2" license="$3" tmp added=0
  tmp="$(mktemp -d)"
  echo "[fetch] cloning $repo …"
  git clone --depth 1 -q "https://github.com/$repo" "$tmp/repo"
  while IFS= read -r skill_md; do
    local dir name; dir="$(dirname "$skill_md")"; name="$(basename "$dir")"
    [ -e "$SKILLS/$name" ] && continue                       # never clobber
    [ "$cap" -ne 0 ] && [ "$added" -ge "$cap" ] && break     # respect the cap
    cp -r "$dir" "$SKILLS/$name"                             # whole dir: SKILL.md + bundled files
    added=$((added + 1))
  done < <(find "$tmp/repo" -name SKILL.md | sort)
  rm -rf "$tmp"                                              # remove the clone
  echo "[fetch] $repo: added $added skills (license: $license)"
}

# source lookup as a case statement (not `declare -A`): macOS ships bash 3.2, which has no
# associative arrays
SOURCES="anthropics lambdatest"
lookup() {  # source -> "repo cap license" ("" if unknown)
  case "$1" in
    anthropics)  echo "anthropics/skills 0 per-skill(frontmatter)" ;;
    lambdatest)  echo "LambdaTest/agent-skills 12 MIT" ;;
    # nvidia)      echo "nvidia/skills 30 Apache-2.0" ;;
    # trailofbits) echo "trailofbits/skills 12 CC-BY-SA-4.0" ;;
  esac
}

targets=("$@")
[ "${#targets[@]}" -eq 0 ] && { echo "usage: $0 all | <source> [<source> …]  (sources: $SOURCES)"; exit 1; }
# shellcheck disable=SC2206
[ "${targets[0]}" = "all" ] && targets=($SOURCES)

for t in "${targets[@]}"; do
  spec="$(lookup "$t")"
  [ -z "$spec" ] && { echo "unknown source '$t' (have: $SOURCES)"; exit 1; }
  # shellcheck disable=SC2086
  fetch $spec
done

echo "[fetch] $(find "$SKILLS" -name SKILL.md | wc -l | tr -d ' ') skills now in $SKILLS"
echo "[fetch] restart the server to pick them up:  docker compose restart mcp"
