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
4. Run the repository's Code Health safeguard when available and fix regressions.

Report security issues through the private process in [SECURITY.md](SECURITY.md), not a public issue.
