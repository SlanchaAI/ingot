# Skills-Only Scope Correction

Date: 2026-07-15
Status: approved by operator

## Outcome

Skill Router remains the existing MCP/Docker application for discovering, creating, using, and
improving Agent Skills. This change adds safer one-call routing and stronger behavioral promotion
evidence without introducing a packaged CLI, a PyPI distribution, harness plugins, or integrations
with separate products.

## Compatibility boundary

The existing five MCP tools remain available: `list_skills`, `suggest_skills`, `get_skill`,
`create_skill`, and `reload_skills`. `route_and_load` becomes an additive sixth tool for clients that
want one selection-and-load round trip. Existing Docker commands, HTTP transport, demo agent,
optimizer, approval UI, and skill-authoring behavior remain the primary workflow.

No new `skill-router` console command is installed. Python module and Docker Compose entrypoints stay
unchanged from `master`.

## Skill roots

`SKILL_ROUTER_PATHS` may add shared read-only roots. The repository `skills/` directory is always
included last as the writable authoring root, so `create_skill` remains live and newly created skills
remain routable. Earlier roots win duplicate names with a visible warning.

## Improvement safety

Keep held-out split enforcement, routing regression metrics, full-skill revision binding, evidence
artifacts, snapshots, and rollback-protected promotion. These strengthen the existing optimizer and
approval loop; they do not reposition the repository as a benchmark product.

## Explicit removals

- Packaged CLI and CLI tests.
- `pyproject.toml`, wheel-first install story, and package-specific Docker changes.
- Claude/Codex adapter bundles that depend on the packaged command.
- Enterprise/product-boundary copy and implementation.
- Empty source lock and disabled fetch flow that break the existing quick start.
- Wholesale README rewrite; documentation returns to the existing product story with a concise
  additive section for `route_and_load` and Behavioral CI hardening.

## Verification

- Existing five MCP tools plus `route_and_load` are registered.
- Existing demo agent still uses suggest/load/create behavior.
- Docker Compose still starts the MCP service through `python -m mcp_server.server` over HTTP.
- External roots and local authoring coexist.
- No tracked text contains names belonging to separate products.
- No `skill-router` console entrypoint or CLI module remains.
- Full tests and Compose configuration pass.
