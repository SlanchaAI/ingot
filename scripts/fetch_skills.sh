#!/usr/bin/env bash
# Fetch example Agent Skills (SKILL.md format) and QUARANTINE them for review. Every source is
# OPTIONAL and nothing is redistributed in this repo, each source is cloned from upstream, its
# skills submitted for review, and the clone deleted, so skills stay under their own upstream
# licenses.
#
# Nothing here is served. This used to copy third-party directories straight into ./skills, where
# the server picked them up on the next restart — an install, with no review and no record of what
# changed. Every package now goes through `ingot add`, which reviews it, records where it came
# from, and leaves the served library byte-identical until a human approves it.
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

if ! command -v ingot >/dev/null 2>&1; then
  echo "fetch_skills.sh needs the \`ingot\` command (pip install -e .)." >&2
  echo "It quarantines each package for review instead of copying it into the served library." >&2
  exit 1
fi

# quarantine up to $cap skill dirs (0 = no cap) from a freshly-cloned repo
fetch() {  # repo  cap  license
  local repo="$1" cap="$2" license="$3" tmp added=0 refused=0
  tmp="$(mktemp -d)"
  echo "[fetch] cloning $repo …"
  git clone --depth 1 -q "https://github.com/$repo" "$tmp/repo"
  while IFS= read -r skill_md; do
    local dir; dir="$(dirname "$skill_md")"
    [ "$cap" -ne 0 ] && [ "$added" -ge "$cap" ] && break     # respect the cap
    if ingot add "file:$dir" >/dev/null; then                # whole dir: SKILL.md + bundled files
      added=$((added + 1))
    else
      refused=$((refused + 1))                               # already present, or refused on review
    fi
  done < <(find "$tmp/repo" -name SKILL.md | sort)
  rm -rf "$tmp"                                              # remove the clone
  echo "[fetch] $repo: quarantined $added, refused or skipped $refused (license: $license)"
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

echo "[fetch] nothing is served yet. Review what arrived and approve what you want:"
echo "[fetch]   ingot list                    # unchanged until an approval publishes"
echo "[fetch]   open http://localhost:8080    # the change-control console"
