# Whole-repository verification

Run from the checkout with Python 3.12+, Node 22+, Docker, and the pinned requirements:

```sh
python -m pip install -r requirements.txt
# Public model data is a test dependency, not a model API call. Prepare once before offline tests.
HF_HOME="$PWD/.whole-tests/huggingface" python -c 'from mcp_server.embedding import build_embedding; build_embedding()'
bash tests/verify_whole_tests.sh
```

For offload storage, set `WHOLE_TEST_ARTIFACTS` to that directory and prepare the
model with `HF_HOME="$WHOLE_TEST_ARTIFACTS/huggingface"`. Set `PYTHON` if the test
interpreter is not `python3`. Everything created by the verifier stays under its
artifact directory. Existing services are untouched: a disposable, resource-limited
Keycloak binds a random loopback port and is removed on exit. The public Keycloak
image must already be present or pullable. The verifier waits while one-minute load
is 16 or higher; pytest runs serially.

`whole_tests_inventory.json` maps every product Python module, shell script and
inline JavaScript module to behavior tests. Discovery uses Git's tracked and
nonignored new files, independently of the map. New source modules or stale map
entries fail verification. Empty package markers and the synthetic routing-project
input are individually explained in the map. Test files, instrumentation, and the
verifier are individually identified as verification infrastructure rather than
product modules. They are not blanket product coverage exclusions. Configuration,
Markdown, CSS, JSON fixtures and CI declarations are not executable modules. There
are no vendored/generated product modules in this checkout.

Python coverage names every package as a source, so even unimported modules appear
with zero coverage. Branches and per-test dynamic contexts are collected. The audit
requires executed function behavior attributed to a mapped passing test for each
nonempty module; imports alone cannot satisfy it. The aggregate coverage floor is
85%, not a claim of exhaustive branch coverage. The sandbox driver has direct real
child-process tests because ordinary subprocess tests do not contribute parent
process coverage.

Shell tests run real script bodies with fake external CLI/HTTP boundaries, isolated
homes and synthetic data. Bash xtrace goes to a separate evidence file; the audit
requires executed source lines for each script. These are orchestration tests, not
claims that installed model clients or hosted tracing services were contacted.
Browser tests execute all inline scripts from the actual HTML in Node's VM with a
synthetic DOM and fetch transport. Native V8 precise coverage records all three
scripts, including uncalled functions. Assertions cover initial authentication,
rendering/escaping, API success/errors, action errors, and polling recovery. This is
JavaScript behavior coverage, not browser layout or visual coverage.

The complete pytest suite includes real isolated Keycloak login, signature rejection
and callback tests. Any skipped/error/failing test rejects the audit. The verifier
itself has negative tests for unmapped modules, omitted coverage, import-only
execution, wrong test attribution and incomplete suites. No assertions are weakened
and no failures are skipped.

CI executes the same verifier, uploads Python JSON/XML/HTML coverage, V8 coverage,
shell traces, inventory, JUnit results and the final audit. Existing image-build and
Compose smoke checks remain in CI. These latter checks exercise an isolated CI
runner; the local verifier does not start the full application stack.
