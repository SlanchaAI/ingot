#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# All caches/artifacts belong to the checkout unless the caller supplies offload storage.
ARTIFACTS=${WHOLE_TEST_ARTIFACTS:-$PWD/.whole-tests}
mkdir -p "$ARTIFACTS"
ARTIFACTS=$(cd "$ARTIFACTS" && pwd)
export TMPDIR="$ARTIFACTS/tmp"
mkdir -p "$TMPDIR"
export HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 LITELLM_LOCAL_MODEL_COST_MAP=True
export HF_HOME="$ARTIFACTS/huggingface" XDG_CACHE_HOME="$ARTIFACTS/cache"
export COVERAGE_FILE="$ARTIFACTS/.coverage" WHOLE_SHELL_TRACE="$ARTIFACTS/shell.trace"
PYTHON=${PYTHON:-python3}
"$PYTHON" --version
"$PYTHON" -m pytest --version
"$PYTHON" -m coverage --version
node --version
docker --version
taskset --version
# Do not run an expensive suite on an overloaded shared worker.
while ! "$PYTHON" -c 'import os,sys; sys.exit(os.getloadavg()[0] >= 16)'; do
  echo 'Waiting: one-minute load >= 16'
  sleep 30
done
"$PYTHON" tests/whole_tests_audit.py inventory "$ARTIFACTS"
container=''
cleanup() {
  if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null; fi
}
trap cleanup EXIT
# Own only this disposable provider. No existing Compose project/services are touched.
container=$(docker run -d --rm --cpus=2 --memory=1g \
  -p 127.0.0.1::8081 \
  -v "$PWD/ops/keycloak:/opt/keycloak/data/import:ro" \
  quay.io/keycloak/keycloak:26.0 start-dev --import-realm --http-port=8081)
port=$(docker port "$container" 8081/tcp)
export KEYCLOAK_URL="http://$port"
export KEYCLOAK_REALM=ingot
"$PYTHON" - <<'PY'
import os, time, urllib.request
url = os.environ['KEYCLOAK_URL'] + '/realms/ingot/.well-known/openid-configuration'
for attempt in range(90):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status == 200:
                print('Isolated Keycloak ready:', url)
                break
    except OSError:
        time.sleep(1)
else:
    raise SystemExit('Isolated Keycloak unavailable after 90 attempts')
PY
: > "$WHOLE_SHELL_TRACE"
"$PYTHON" -m coverage erase
cpus=$("$PYTHON" -c 'import os; print(",".join(map(str, sorted(os.sched_getaffinity(0))[:2])))')
taskset -c "$cpus" "$PYTHON" -m coverage run -m pytest tests -q \
  --basetemp="$ARTIFACTS/pytest" --junitxml="$ARTIFACTS/pytest.xml"
"$PYTHON" -m coverage json --show-contexts -o "$ARTIFACTS/coverage.json"
"$PYTHON" -m coverage xml -o "$ARTIFACTS/coverage.xml"
"$PYTHON" -m coverage html -d "$ARTIFACTS/html"
"$PYTHON" -m coverage report
node tests/browser_behavior.cjs "$ARTIFACTS/inline-scripts.json" "$ARTIFACTS/browser-coverage.json"
"$PYTHON" tests/whole_tests_audit.py audit "$ARTIFACTS"
echo "Whole-repository verification passed. Evidence: $ARTIFACTS"
