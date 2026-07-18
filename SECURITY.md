# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's
[private vulnerability report](https://github.com/SlanchaAI/ingot/security/advisories/new) and
include affected versions, impact, reproduction steps, and any proposed mitigation. Do not include
real credentials, private prompts, or sensitive traces.

The latest `master` revision receives security fixes. Older revisions may need to upgrade first.

## Trust boundaries

Ingot is a local development system, not a hardened multi-tenant service.

- MCP and approval UI endpoints have no built-in authentication. Docker publishes them on
  `127.0.0.1` by default. Add authenticated transport before exposing them to a network.
- Agent-authored `create_skill` calls only queue inactive candidates. A normal application flow can
  activate a new skill or rewrite only through an explicit approval UI action.
- Content checks on agent-authored skills are defense in depth, not proof that a skill is safe.
- Optimizer-generated changes only create pending recommendations. Reviewers own every
  activation decision.
- Fetched third-party skills are dependencies. Review their code, instructions, and licenses.
- Run agents without sensitive host mounts. Keep real keys only in the gitignored `.env` file.
- The execution sandbox reduces risk; it does not make arbitrary instructions trustworthy.

For production use, add authentication, authorization, audit logging, rate limits, isolated tool
execution, secret scoping, and a human-reviewed skill publication path.

Direct operator edits under `skills/` bypass the application workflow and therefore remain a
trusted-administrator action, not a human-presence guarantee enforced by Ingot.
