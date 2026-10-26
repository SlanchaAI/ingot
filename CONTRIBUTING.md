# Contributing

Thanks for improving Ingot.

## Development

Run builds and tests in Docker:

```bash
docker compose build
docker run --rm -v "$PWD:/app" -w /app ingot-mcp python -m pytest tests -q
```

Keep changes small, add tests for behavior changes, and preserve existing style. Never commit real
keys, private traces, or generated `.env` files. The README tutorial numbers must come from an
actual recorded run; do not estimate or rewrite them for marketing.

Before opening a pull request:

1. Run the full Docker test suite.
2. Confirm `docker compose config --quiet` succeeds when Compose files change.
3. Describe user-visible behavior, risks, and verification evidence.
4. Run CodeScene only when the pull request or reviewer explicitly asks for it.

Report security issues through the private process in [SECURITY.md](SECURITY.md), not a public issue.

## Whole-source checks

Use Python 3.12 and Node 22. Install runtime dependencies and pinned checkers, then run the same
command required by the pull-request lint job:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-lint.txt
npm ci
bash tests/verify_whole_lint.sh
```

The verifier inventories tracked and unignored source files, prints versions and exact coverage,
and fails on any checker error. Python packages `agent`, `mcp_server`, `optimize`, `ui`, all tests,
and the evaluation fixture run through Ruff and mypy (`check_untyped_defs`). Shell scripts run
through ShellCheck and Bash syntax checks. JavaScript, including all inline browser scripts,
HTML event handlers, and the Node setup expression, runs through ESLint and strict TypeScript
`checkJs` without emitting output. HTML uses HTML-validate's recommended rules; embedded CSS uses
Stylelint's recommended rules. Shell, HTML, and CSS have no separate static type system.
Embedded Python in shell commands and Docker heredocs receives the same Ruff/mypy checks.

No first-party source is excluded. Vendored `optimize/skillopt_prompts/README.md`,
`analyst_error.md`, `lr_autonomous.md`, `ranking.md`, and `slow_update.md` are attributed prompt data rather than
executable code. `.venv/` and `node_modules/` contain installed dependencies; `.cache/` contains
extracted source copies and checker caches. These generated directories are ignored by Git and
Docker. Optional fetched `skills/*` and generated `optimize/tasks/*.yaml` remain runtime data
under their existing Git exclusions. The verifier checks original source via extracted copies;
it never executes optimization, model APIs, or setup/smoke scripts.
