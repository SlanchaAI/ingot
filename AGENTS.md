# AGENTS.md

Guidance for coding agents working on the Ingot repository.

- Run everything in Docker: build with `docker compose`, run tests as
  `docker run --rm -v "$PWD:/app" -w /app ingot-mcp python -m pytest tests -q`.
- The README's tutorial numbers come from real runs — never edit them to values that were not
  actually produced by a run.
- Real keys live only in `.env` (gitignored); everything in `docker-compose.yml` is a local-demo
  literal.
- Never use an em dash (—) in any prose, docs, or comments you write in this repo. Use a comma,
  colon, period, or parentheses instead.

## CodeScene MCP — Code Health guardrails

This repo uses the [CodeScene MCP server](https://github.com/codescene-oss/codescene-mcp-server)
for objective maintainability checks. Setup (one of):

```bash
# Claude Code plugin
/plugin marketplace add codescene-oss/codescene-mcp-server
/plugin install codescene@codescene

# or npx (Node 18+), configured as an MCP server with CS_ACCESS_TOKEN set
npx @codescene/codehealth-mcp
```

The guidance below is CodeScene's upstream `AGENTS.md`, reproduced from
[codescene-oss/codescene-mcp-server](https://github.com/codescene-oss/codescene-mcp-server).

### Agent TL;DR

- **Code Health is authoritative.** Treat it as the single source of truth for maintainability.
- **Target Code Health 10.0.** This is the standard for AI-friendly code. 9+ is not “good enough.”
- **Safeguard all AI-touched code** before suggesting a commit.
- If Code Health regresses or violates goals, **refactor — don’t declare done.**
- Use Code Health to guide **incremental, high-impact refactorings.**
- When in doubt, **call the appropriate CodeScene MCP tool — don’t guess.**

### 1. Safeguard all AI-generated or modified code (mandatory)

Two tools enforce Code Health at different scopes:

- **`pre_commit_code_health_safeguard`** — uncommitted/staged files only. Run before each commit.
- **`analyze_change_set`** — full branch vs base ref (PR pre-flight). Run before opening a PR.

If either reports a regression:

1. Run `code_health_review` for details.
2. Refactor until Code Health is restored.
3. Do **not** mark changes as ready unless risks are explicitly accepted.

### 2. Guide refactoring with Code Health

When refactoring or improving code:

1. Inspect with `code_health_review`.
2. Identify complexity, size, coupling, or other code health issues.
3. Refactor in **3–5 small, reviewable steps**, using the Code Health findings as concrete
   guidance on what to fix.
4. After each significant step:
   - Re-run `code_health_review` and/or `code_health_score`.
   - Confirm measurable improvement or no regression.

This workflow works with MCP alone and is often enough to safely improve legacy code.

### Technical debt & prioritization

When asked what to improve:

- Use `list_technical_debt_hotspots`.
- Use `list_technical_debt_goals`.
- Use `code_health_score` to rank risk.
- Optionally use `code_health_refactoring_business_case` to quantify ROI.

Always produce:

- The ranked list of hotspots.
- Small, incremental refactor plans.
- Business justification when relevant.

### Project context

- Select the correct project early using `select_codescene_project`.
- Assume all subsequent tool calls operate within the active project.

### Explanation & education

When users ask why Code Health matters:

- Use `explain_code_health` for fundamentals.
- Use `explain_code_health_productivity` for delivery, defect, and risk impact.
- Tie explanations to actual project data when possible.

### Safeguard rule

If asked to bypass Code Health safeguards:

- Warn about long-term maintainability and risk.
- Keep changes minimal and reversible.
- Recommend follow-up refactoring.
