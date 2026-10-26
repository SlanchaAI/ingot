#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Keep temporary files and checker caches inside the checkout on every platform.
mkdir -p .cache/whole-lint/tmp
export TMPDIR="$PWD/.cache/whole-lint/tmp"
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export RUFF_CACHE_DIR="$PWD/.cache/ruff"
export MYPY_CACHE_DIR="$PWD/.cache/mypy"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
if [[ -x .venv/bin/python ]]; then
  export PATH="$PWD/.venv/bin:$PATH"
fi
python tests/whole_lint.py
