# Privacy & security

## Privacy first

Three properties, all defaults, none optional:

- **Zero data retention LLM calls.** The default provider is **OpenRouter** with
  [Zero Data Retention (ZDR)](https://openrouter.ai/docs/features/zdr) enforced on every request.
  Each call (agent runs, candidate rollouts and reflection, the judge, task drafting) carries a
  hardcoded provider preference:

  ```json
  {"provider": {"zdr": true, "data_collection": "deny"}}
  ```

  OpenRouter then routes only to ZDR endpoints operated by providers that do not collect user
  data. A model with no qualifying endpoint fails loudly rather than falling back to one that
  retains prompts. Provider-direct endpoints work too: Fireworks AI, for example, is
  [zero-data-retention by default](https://docs.fireworks.ai/guides/security_compliance/data_handling)
  for open models on serverless, under its own retention policy.
- **Self-hosted tracing.** The default evals backend is a self-hosted Langfuse (Postgres,
  ClickHouse, MinIO) that runs inside the compose stack, so traces stay on your machine (hosted
  inference is the separate exception noted below). Its ports are bound to `127.0.0.1` and its
  datastores publish no host port; before exposing it beyond your machine, rotate the demo
  credentials; see [Securing the Langfuse deployment](#securing-the-langfuse-deployment).
- **Localhost only.** No service is reachable off the machine (see
  [Network exposure](#network-exposure)).

The only data that leaves your machine is the LLM traffic itself. `BASE_URL` + `API_KEY` point
everything at any OpenAI-compatible provider (`MODEL_BASE_URL`/`MODEL_API_KEY` override just the
serving role):

```bash
# the default (.env.example): OpenRouter, ZDR-only provider routing enforced in code
BASE_URL=https://openrouter.ai/api/v1
API_KEY=sk-or-...

# provider-direct alternative: Fireworks AI (ZDR by default for open models on serverless)
BASE_URL=https://api.fireworks.ai/inference/v1
API_KEY=fw_...
AGENT_MODEL=accounts/fireworks/models/qwen3p7-plus
SKILLOPT_MODEL=accounts/fireworks/models/glm-5p2
JUDGE_MODEL=accounts/fireworks/models/deepseek-v4-pro

# fully local (no key needed at all): everything on Ollama / vLLM
BASE_URL=http://172.17.0.1:11434/v1  AGENT_MODEL=qwen3:32b  SKILLOPT_MODEL=qwen3:32b  JUDGE_MODEL=llama3.3:70b
```

No API key is required when nothing points at a hosted endpoint. From inside the compose
containers, "localhost" is the container itself; use your host's LAN IP (or `172.17.0.1` on Linux).


## Security & threat model

**A loaded skill is instructions the agent follows.** Treat skill content as code, and the skills
library as trusted state that must be curated. You cannot fully "solve" prompt injection in a
system whose job is to retrieve and follow instructions; the design goal is proportionate
guardrails plus a small, well-defended write surface.

Write paths, and what guards each:

- **Generated rewrites** land in `runs/pending/` and cannot activate themselves. They also require
  evidence whose champion and challenger revisions still match the skill on disk before UI approval.
- **Approval and rollback** are the only application paths that write under `skills/`. Both go
  through `optimize/promote.py`, both snapshot what they displace, and both append an audit record
  on a best-effort basis (a failed append is logged and does not undo the committed change).
  The UI endpoints that trigger them carry a same-origin check, because a cross-site page can POST
  to localhost even though it cannot read the response, and only one of them runs at a time in a
  given UI process: a second approval or rollback is refused with HTTP 409 rather than interleaved.
- **Direct filesystem edits** let an operator control trusted state under `skills/`. This explicit
  escape hatch sits outside the application approval guarantee.
- **Third-party skills** are unaudited but not attacker-controlled at runtime; review them as you
  would any dependency.

### Network exposure

**Everything is localhost-only by default, because nothing is authenticated.** The MCP tools and the
change-control UI's endpoints (which can trigger paid candidate runs, activate a skill, or roll one
back) have no auth of their own; the default protection is that no service is reachable off the
machine:

- `docker-compose.yml` publishes every port on loopback only (`127.0.0.1:8000` MCP,
  `127.0.0.1:8080` UI, `127.0.0.1:3100` Langfuse).
- Run outside Docker, the MCP server also binds `127.0.0.1` by default.

To expose a service, change its port mapping in `docker-compose.yml` from `"127.0.0.1:8000:8000"`
to `"8000:8000"` (or bind a specific interface). Do this knowingly: anyone who can reach those
ports can queue candidates, approve activations, roll skills back, and spend your API budget.

**The change-control UI has a ready-made path.** The `lan` profile runs a Caddy TLS front door
that publishes only the UI, on every interface, while the UI itself stays loopback-bound:

```bash
docker compose --profile lan up -d proxy    # then browse to https://<this-box's-name-or-IP>
```

There is nothing to register or attach: Caddy mints certificates on demand from its own local CA,
so credentials never cross the network in cleartext. Browsers show a one-time "connection not
private" warning for the unknown CA; proceed past it, trust Caddy's root certificate (in the
`caddy_data` volume at `/data/caddy/pki/authorities/local/root.crt`) to remove it, or swap real
certificates into `ops/caddy/Caddyfile`. The UI keeps its password gate (default `admin`/`ingot`,
pinned non-empty in `docker-compose.yml`); change `AUTH_PASSWORD` in `.env` before pointing
teammates at it.

**Sharing the UI on a trusted LAN?** Turn on the built-in password gate so approvals are gated and
attributable, add a user and the change-control UI requires HTTP Basic auth (against a local
`runs/auth.json` of salted PBKDF2 hashes), and each approval or rollback records that username as
the audit `actor` instead of `local-operator`:

```bash
docker compose run --rm ui python -m ui.auth add alice   # prompts for a password; auth is now ON
```

It's off until the first user exists (the local default stays zero-config). This is LAN-grade:
Basic credentials ride every request, so keep the server inside your network boundary and add TLS
if you can. For a shared or company-wide deployment, [Sign in with Google](sso.md)
(`AUTH_MODE=oidc`) adds domain-restricted login and the viewer/proposer/approver/admin roles, with
the signed-in email as the audit actor. For authenticating the MCP serving endpoints themselves, put
an authenticating reverse proxy in front.

### Securing the Langfuse deployment

The self-hosted Langfuse evals backend comes up with `docker compose up`, so its exposure matters.
Two things keep the defaults safe on a single machine:

- **Nothing is published beyond loopback.** Only `langfuse-web` maps a host port, on
  `127.0.0.1:3100`; Postgres, ClickHouse, MinIO, and Redis publish no host port at all and are
  reachable only on the internal compose network.
- **Every credential is a demo literal you can override.** The bundled `SALT`, `ENCRYPTION_KEY`
  (all-zeros), `NEXTAUTH_SECRET`, datastore passwords, project API keys (`pk/sk-lf-local-demo`), and
  web login (`demo@local.dev` / `localdemo123`) are safe *only* because of the loopback binding above.

Before exposing Langfuse beyond your machine, rotate every demo secret and use its separate,
opt-in TLS endpoint. The normal `lan` proxy publishes only the change-control UI, so sharing the UI
does not also publish traces. For a new deployment:

1. Copy every value in `.env.example` under "LANGFUSE SECURITY" into `.env` and replace the demo
   values before the first `docker compose up`. At minimum set `LANGFUSE_ENCRYPTION_KEY`,
   `LANGFUSE_SALT`, `LANGFUSE_NEXTAUTH_SECRET`, `LANGFUSE_INIT_USER_PASSWORD`, every datastore
   credential, and the Langfuse project keys.
2. Set `LANGFUSE_PUBLIC_URL=https://<this-box's-name-or-IP>:3443` in `.env`. This must match the URL
   used by browsers so Langfuse generates the correct authentication URLs.
3. Run `docker compose --profile langfuse-lan up -d langfuse-proxy`, then browse to that URL. Caddy
   uses its local CA, with the same trust considerations described under Network exposure.

The `.env` defaults initialize a new deployment; they are not a general credential-rotation
mechanism. `LANGFUSE_INIT_*` values create resources only when they do not already exist, and the
Postgres image applies `POSTGRES_PASSWORD` only when it initializes an empty data directory. On a
disposable demo, stop Compose, remove the four `ingot_langfuse_*_data` volumes and the
`ingot_langfuse_clickhouse_logs` volume, set `.env`, and start again. This permanently deletes
existing traces. On a deployment whose traces must survive, back up
the datastores, rotate database and object-store credentials with their native administration tools,
rotate the user and project credentials in Langfuse, update `.env`, and then restart the stack. Do
not replace `LANGFUSE_ENCRYPTION_KEY` or `LANGFUSE_SALT` on an initialized deployment without the
corresponding Langfuse migration procedure. Langfuse documents the create-only behavior in its
[headless initialization guide](https://langfuse.com/self-hosting/administration/headless-initialization).
An all-zeros `ENCRYPTION_KEY` makes encryption of stored secrets worthless if the volumes leak.

Deliberately not done: we do not scan or denylist skill content (shell commands, `.env` mentions,
`curl … | sh`), because legitimate skills routinely contain code and install steps; human review at
the approval step is the content check. Contain the residual risk operationally: run the agent in a
container without real secrets or sensitive host paths. Further reading:
[OpenAI on prompt injection](https://openai.com/safety/prompt-injections/).
