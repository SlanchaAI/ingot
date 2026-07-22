# Production setup

This guide deploys Ingot with the bundled open source Langfuse stack and enrolls Claude Code or
Codex agents running on other machines. The example assumes a trusted private network and an Ingot
host at `192.168.1.40`. Replace that address with a stable DNS name or the private address of your
host.

## Production shape

- The Ingot host runs MCP, the change-control UI, Langfuse, Postgres, ClickHouse, Redis, and MinIO
  with Docker Compose.
- Langfuse is exposed through the opt-in Caddy TLS endpoint on port `3443`.
- MCP is exposed only on a trusted private interface on port `8000`.
- Each agent machine runs its local Claude Code or Codex connector and sends traces to Langfuse.
- Langfuse datastores stay on the internal Compose network and publish no host ports.

MCP does not have built-in authentication. Use a private LAN, a host firewall, a VPN such as
Tailscale, or an authenticating reverse proxy. Never expose port `8000` directly to the internet.

## 1. Prepare the Ingot host

Install Git, Docker with Compose 2.24.4 or newer, and OpenSSL. Clone the repository, then create the
production environment file:

```bash
git clone https://github.com/SlanchaAI/ingot.git
cd ingot
cp .env.example .env
chmod 600 .env
```

Before the first start, edit `.env` and set all of these values. Keep real values only in `.env`:

```dotenv
LANGFUSE_PUBLIC_URL=https://192.168.1.40:3443
LANGFUSE_HOST=192.168.1.40
LANGFUSE_BIND_ADDRESS=192.168.1.40
LANGFUSE_PUBLIC_KEY=pk-lf-replace-me
LANGFUSE_SECRET_KEY=sk-lf-replace-me
LANGFUSE_ENCRYPTION_KEY=replace-with-64-hex-characters
LANGFUSE_SALT=replace-with-random-value
LANGFUSE_NEXTAUTH_SECRET=replace-with-random-value
LANGFUSE_INIT_USER_EMAIL=operator@example.com
LANGFUSE_INIT_USER_PASSWORD=replace-with-a-strong-password
POSTGRES_PASSWORD=replace-with-a-random-password
CLICKHOUSE_PASSWORD=replace-with-a-random-password
MINIO_ROOT_USER=replace-with-a-random-user
MINIO_ROOT_PASSWORD=replace-with-a-random-password
REDIS_AUTH=replace-with-a-random-password

AUTH_USER=admin
AUTH_PASSWORD=replace-with-a-strong-password

BASE_URL=https://openrouter.ai/api/v1
API_KEY=sk-or-replace-me
AGENT_MODEL=qwen/qwen3-32b
SKILLOPT_MODEL=z-ai/glm-5.2
JUDGE_MODEL=google/gemini-2.5-flash
OPENROUTER_PROVIDERS=groq
```

Generate suitable random values with OpenSSL:

```bash
openssl rand -hex 32  # LANGFUSE_ENCRYPTION_KEY and LANGFUSE_NEXTAUTH_SECRET
openssl rand -hex 16  # salts, API key suffixes, and service passwords
```

OpenRouter is the supported hosted model path. Provider priorities are preferences, not an
allowlist: if Groq does not serve a configured role, Ingot falls back to another ZDR-qualified
OpenRouter endpoint. Fully local vLLM or Ollama is also supported as described in
[Configuration](docs/configuration.md).
For a shared team deployment, consider OIDC for the change-control UI as described in
[SSO](docs/sso.md).

Initialization values create the initial Langfuse resources only on a new datastore. Changing
`.env` later does not rotate existing database passwords, project keys, or users. Follow the
rotation and backup guidance in [Security](docs/security.md#securing-the-langfuse-deployment) for
an existing installation.

## 2. Expose MCP on the private interface

The default Compose file binds MCP to loopback. Create an untracked production override that binds
it only to the host's trusted interface:

```yaml
# compose.production.yml
services:
  mcp:
    ports: !override
      - "192.168.1.40:8000:8000"
```

The checked-in Langfuse proxy binds `3443` to `LANGFUSE_BIND_ADDRESS`, which the previous step set
to the trusted interface. Restrict inbound TCP `8000` and `3443` to the agent subnet or VPN in the
host firewall. Do not expose Postgres, ClickHouse, Redis, or MinIO.

## 3. Start open source Langfuse and Ingot

Start the default application plus the separate Langfuse TLS front door:

```bash
docker compose -f docker-compose.yml -f compose.production.yml \
  --profile langfuse-lan up -d --build
```

Check container health and both remote endpoints:

```bash
docker compose -f docker-compose.yml -f compose.production.yml ps
curl -k https://192.168.1.40:3443/api/public/health
curl http://192.168.1.40:8000/mcp
```

The MCP request can return a protocol error because a plain `curl` request is not an MCP session.
An HTTP response still proves that the listener is reachable.

Open `https://192.168.1.40:3443`, sign in with `LANGFUSE_INIT_USER_EMAIL` and
`LANGFUSE_INIT_USER_PASSWORD`, and confirm that the `ingot` project exists.

## 4. Trust the Langfuse TLS certificate

The bundled Caddy endpoint uses its own local certificate authority. Copy its root certificate from
the Ingot host:

```bash
docker compose cp \
  langfuse-proxy:/data/caddy/pki/authorities/local/root.crt \
  ./ingot-caddy-root.crt
```

Transfer `ingot-caddy-root.crt` to each agent machine over an authenticated channel and add it to
that machine's system trust store. Restart Claude Code or Codex after changing trust. A certificate
warning should not be bypassed for unattended production agents. A DNS name with a certificate from
your organization's CA or a public CA is preferable when available.

From every agent machine, verify both endpoints before enrollment:

```bash
curl https://192.168.1.40:3443/api/public/health
curl http://192.168.1.40:8000/mcp
```

## 5. Enroll a remote Claude Code agent

On the Claude Code machine, install Claude Code and `uv` (recommended). The fallback is Python 3.10
or newer with `pip` and `langfuse>=4.0,<5`. Copy or clone this repository so the setup script is
available, then run:

```bash
INGOT_MCP_URL=http://192.168.1.40:8000/mcp \
LANGFUSE_BASE_URL=https://192.168.1.40:3443 \
LANGFUSE_PUBLIC_KEY=pk-lf-replace-me \
LANGFUSE_SECRET_KEY=sk-lf-replace-me \
./scripts/claude_setup.sh
```

Restart Claude Code. The setup command passes the Langfuse URL, `LANGFUSE_PUBLIC_KEY`, and
`LANGFUSE_SECRET_KEY` stored on the Ingot host to the plugin. Then diagnose the local installation:

```bash
INGOT_MCP_URL=http://192.168.1.40:8000/mcp \
LANGFUSE_BASE_URL=https://192.168.1.40:3443 \
LANGFUSE_PUBLIC_KEY=pk-lf-replace-me \
LANGFUSE_SECRET_KEY=sk-lf-replace-me \
./scripts/claude_setup.sh --doctor
```

Use `--repair` with the same environment if the MCP URL is wrong or a managed connector dependency
is incomplete.

## 6. Enroll a remote Codex agent

On the Codex machine, install Codex 0.128 or newer, Node.js 22 or newer, and Python 3. Then run:

```bash
INGOT_MCP_URL=http://192.168.1.40:8000/mcp \
LANGFUSE_BASE_URL=https://192.168.1.40:3443 \
LANGFUSE_PUBLIC_KEY=pk-lf-replace-me \
LANGFUSE_SECRET_KEY=sk-lf-replace-me \
./scripts/codex_setup.sh
```

The script writes the connector credentials to `~/.codex/langfuse.json` with mode `0600`. Restart
Codex, then run the non-mutating diagnostic:

```bash
INGOT_MCP_URL=http://192.168.1.40:8000/mcp \
LANGFUSE_BASE_URL=https://192.168.1.40:3443 \
./scripts/codex_setup.sh --doctor
```

Use `--repair` with the complete setup environment if the MCP URL or plugin state must be replaced.

## 7. Verify enrollment

Ask each agent to call `ingot.route_and_load` once at the start of a request and follow the returned
`skill_body`. Confirm all of the following:

1. The agent can list and call the `ingot` MCP tools.
2. A trace appears in the Langfuse `ingot` project.
3. The trace includes the user task, final response, and `route_and_load` tool call.
4. `./scripts/claude_setup.sh --doctor` or `./scripts/codex_setup.sh --doctor` passes on the agent.

Connecting MCP exposes the tools but does not force their use. Add the route-and-load instruction to
the agent's persistent project instructions. Use `CLAUDE.md` for Claude Code and `AGENTS.md` for
Codex, with the exact rule in [Make skill loading part of the agent instructions](docs/mcp-integration.md#make-skill-loading-part-of-the-agent-instructions).

## Operations

Back up all named Langfuse datastore volumes and the repository's `skills/`, `runs/`, and
`optimize/tasks/` directories. Pin image versions, review upgrades before applying them, and test
restore procedures. Monitor container health and disk usage:

```bash
docker compose -f docker-compose.yml -f compose.production.yml ps
docker compose -f docker-compose.yml -f compose.production.yml logs --tail=200
docker system df
```

Upgrade by taking backups, pulling reviewed image versions, rebuilding Ingot, and checking health
before enrolling more agents. Keep project API keys distinct from the Langfuse web login, rotate
them when an agent machine is retired, and remove or repair that machine's connector configuration.

For the complete threat model and credential-rotation caveats, read [Security](docs/security.md).
