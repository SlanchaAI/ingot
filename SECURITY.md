# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's
[private vulnerability report](https://github.com/SlanchaAI/ingot/security/advisories/new) and
include affected versions, impact, reproduction steps, and any proposed mitigation. Do not include
real credentials, private prompts, or sensitive traces.

The latest `master` revision receives security fixes. Older revisions may need to upgrade first.

See [ARCHITECTURE.md](ARCHITECTURE.md) for component flows, stores, invariants, and recovery.

## Trust boundaries

Ingot is a local development system, not a hardened multi-tenant service.

- The MCP endpoints have no built-in authentication; Docker publishes them on `127.0.0.1` by
  default, so add authenticated transport before exposing them to a network. The change-control UI
  has a built-in password gate (HTTP Basic, default `admin`/`ingot` under Docker) and an optional
  Sign in with Google mode (OIDC + roles, `AUTH_MODE=oidc`; see [SSO](docs/sso.md)). In the
  zero-config `open` mode it is unauthenticated, so anyone who can reach it can approve a change or
  roll a skill back.
- Candidate generation only creates quarantined pending records. A normal application flow can
  activate a rewrite only through an explicit approval action in the UI; reviewers own every
  activation decision.
- Fetched third-party skills are dependencies. Review their code, instructions, and licenses.
- Run agents without sensitive host mounts. Keep real keys only in the gitignored `.env` file.
- The execution sandbox reduces risk; it does not make arbitrary instructions trustworthy.
- The Docker socket gives a container near-root host authority. It is present only on services that
  launch execution sandboxes. Run them for trusted local operators, use a dedicated Docker context
  or isolated host for stronger separation, and remove the mount when static judging is sufficient.
- Hosted providers receive prompts, skill instructions, and outputs. OpenRouter requests enforce
  ZDR routing, but operators must evaluate every configured provider's policy and jurisdiction.
- Local traces contain task and answer text. Secret-pattern redaction, restrictive permissions,
  rotation, and opt-out are available, but callers should avoid submitting secrets. Protect and
  back up the `runs/` directory according to its data sensitivity.

For production use, add authentication, authorization, audit logging, rate limits, isolated tool
execution, secret scoping, and a human-reviewed skill publication path.

Direct operator edits under `skills/` bypass the application workflow and therefore remain a
trusted-administrator action, not a human-presence guarantee enforced by Ingot.

Approval and rollback actions write metadata-only records to `runs/approval-audit.jsonl`. These
records provide local accountability, not tamper-proof audit storage. Forward them to an append-only
system if regulatory or multi-user assurance is required.

Each record's `actor` is the identity of the approver: the constant `local-operator` in the
zero-config `open` mode, the HTTP Basic username when the LAN password gate is enabled
(`ui/auth.py`), or the signed-in email under Sign in with Google (`AUTH_MODE=oidc`, see
[SSO](docs/sso.md)). In `open` mode the trail can record only that *a* local operator approved a
change, not who; enabling the password or OIDC gate attributes each decision to a person.

The review surface reads recorded evidence bundles through one read-only endpoint. It opens only
the path a pending record wrote, resolves it, and refuses anything that lands outside
`runs/evidence/`, including `..` segments and symlinks out of the tree. Nothing a request carries
selects a file.
