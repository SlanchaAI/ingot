# Ingot

**Evidence-gated change control for agent instructions.**

[![CI](https://github.com/SlanchaAI/ingot/actions/workflows/ci.yml/badge.svg)](https://github.com/SlanchaAI/ingot/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/github/license/SlanchaAI/ingot)](LICENSE) [![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](Dockerfile) [![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](docker-compose.yml)

<p align="center">
  <img src="docs/ingot.jpg" alt="Ingot, the mascot, handing skills out to AI agents" width="640">
</p>

An agent's [skills](https://github.com/anthropics/skills) are instructions it will follow. **Ingot**
is a local-first library that treats them as what they are: versioned state that needs a review
process. Every skill folder is content-addressed, every proposed change is quarantined until a human
reads the evidence and approves it, and promotion is atomic, snapshots what it replaced, and is
recorded. An **MCP server** then serves the approved revision of the right skill for each task,
which is what lets a cheap or local model reuse methods that would otherwise need a frontier model.

What the system guarantees:

- **A revision names an exact skill.** Every file in a skill folder is hashed, so the revision on a
  trace, in a piece of evidence, and on disk are comparable.
- **Changes are quarantined.** Generated rewrites land in `runs/pending/` and cannot route
  traffic until a human approves them.
- **Approval needs evidence.** A rewrite carries held-out champion-vs-challenger scores, per-case
  deltas, token cost, and a gate verdict; promotion re-checks the evidence still matches disk.
- **Promotion is atomic and reversible.** The displaced revision is snapshotted and the directory
  swapped by rename; restore any snapshot from the UI or CLI.
- **Decisions are audited.** Approvals and rollbacks append metadata-only records (action, skill,
  revision, actor, timestamp), never skill text or credentials.

Built for individual users first, ready to share:

- **Batteries included.** `docker compose up` starts the router, the change-control UI, and a
  self-hosted Langfuse for traces and experiments; point `LANGFUSE_*` at your own Langfuse (Cloud or
  self-hosted) to send traces there instead.
- **Local.** Point it at Ollama or vLLM and it runs with no API key; nothing leaves your machine.
- **Secure.** Hosted calls default to OpenRouter with zero-data-retention routing enforced on every
  request, everything binds localhost, and the shared UI has an optional password gate.
- **Easy.** A skill is a folder with a `SKILL.md`. Drop one in and it is live on the next request.

Changes come mostly from you. Ingot also ships an **optional** candidate generator
that mines real traces for failing skills, drafts rewrites, and measures them on held-out tasks; it
produces proposals, never activations.

[Quickstart](#quickstart) · [Tutorial](docs/tutorial.md) · [How it works](docs/how-it-works.md) ·
[Configuration](docs/configuration.md) · [Architecture](ARCHITECTURE.md) ·
[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [MIT license](LICENSE)

## Quickstart

```bash
git clone https://github.com/SlanchaAI/ingot.git && cd ingot
cp .env.example .env               # add an OpenRouter key, or point BASE_URL at Ollama (no key)
scripts/fetch_skills.sh all        # fetch ~70 real skills into ./skills (see docs/skill-sources.md)
docker compose up                  # router (:8000), UI (:8080), Langfuse (:3100), and one agent run
docker compose run --rm agent "How do I merge several PDFs into one and add page numbers?"
```

The change-control UI at `localhost:8080` asks for a login; the compose default is **`admin` /
`ingot`**. Change `AUTH_PASSWORD` in `.env` before sharing it on your LAN (or set it empty to run
open), see [Privacy & security](docs/security.md#network-exposure).

`docker compose up` brings up a self-hosted Langfuse (traces + experiment UI) alongside the router
and UI; trace mining reads from it and has no local fallback, so it fails loudly if no
Langfuse-compatible backend is reachable. Point `LANGFUSE_*` at your own Langfuse (Cloud or
self-hosted) to send traces there instead (the bundled containers keep running unless you stop
them). Backend, model, and gate settings live in [Configuration](docs/configuration.md).

Then walk the full loop, a stale Tailwind skill mined, rewritten, gated, and promoted, in the
[**Tutorial**](docs/tutorial.md).

For a hardened open source Langfuse deployment with agents on other LAN machines, follow
[Production setup](PRODUCTION_SETUP.md).

### Connect Claude Code or Codex

You can use Ingot with your existing coding agent instead of the bundled demo agent. Start the
stack, then run the setup script for your agent. The Codex connector requires Codex 0.128 or newer,
Node.js 22 or newer, and Python 3. The Claude Code connector requires `uv` (recommended), or
Python 3.10 or newer with `pip` and the Langfuse 4.x SDK:

```bash
# macOS, install only the dependencies your selected agent needs
brew install node@22       # Codex
brew install uv            # Claude Code
brew install python@3.12   # Codex when python3 is missing, or Claude's fallback runtime

docker compose up -d
./scripts/claude_setup.sh
# or
./scripts/codex_setup.sh
```

Both scripts are safe to run again. Use `--doctor` to inspect versions, installed connectors,
configuration, and Langfuse reachability without changing anything. Use `--repair` to replace a
mismatched MCP registration and reinstall managed dependencies or plugins:

```bash
./scripts/claude_setup.sh --doctor
./scripts/claude_setup.sh --repair
./scripts/codex_setup.sh --doctor
./scripts/codex_setup.sh --repair
```

Each script adds `http://localhost:8000/mcp` as the user-level `ingot` MCP server and installs the
official Langfuse observability connector. Claude Code prompts for the Langfuse URL and project keys
after restart only when configuration is incomplete. The setup script supplies `LANGFUSE_*` values
or the bundled local defaults during installation. The Codex script writes a private
`~/.codex/langfuse.json`; it defaults to the bundled Langfuse at `http://localhost:3100` with the
local demo project keys.

For Langfuse Cloud or another self-hosted project, provide its values when running the Codex setup:

```bash
LANGFUSE_BASE_URL=https://cloud.langfuse.com \
LANGFUSE_PUBLIC_KEY=pk-lf-... \
LANGFUSE_SECRET_KEY=sk-lf-... \
./scripts/codex_setup.sh
```

Set `INGOT_MCP_URL` if the MCP server is not on localhost. The connectors record prompts, responses,
reasoning summaries, and tool inputs and outputs, including Ingot MCP calls, so do not enable them
for sessions whose contents must not be stored in Langfuse. Their trace-root shapes are accepted by
`optimize-mine`; untagged turns are attributed to skills by task similarity. See
[Bring your own agent](docs/mcp-integration.md) for the trace contract and routing behavior.
Tell the agent to call `ingot.route_and_load` once at the start of each request and follow the
returned `skill_body`; connecting the MCP server makes the tool available but does not force its use.
Add this as a persistent `CLAUDE.md` or `AGENTS.md` rule, not just a one-time chat prompt. See
[Make skill loading part of the agent instructions](docs/mcp-integration.md#make-skill-loading-part-of-the-agent-instructions).
For an agent on another machine, see [Agent on another LAN machine](docs/mcp-integration.md#agent-on-another-lan-machine).
Verify that the services are reachable before starting the agent:

```bash
curl http://localhost:8000/mcp                 # an HTTP error response still proves MCP is listening
curl http://localhost:3100/api/public/health  # should return a healthy Langfuse response
```

## How it works

Ingot does three things around your skill library:

- **Serve.** An MCP server routes each task to the approved revision of the right skill (embedding
  routing on CPU, no GPU) so a weak or local model can reuse a strong method.
- **Govern.** Every change is quarantined, carries held-out evidence, and needs a human approval;
  promotion is atomic, snapshotted, reversible, and audited.
- **Improve.** An optional loop mines real traces for failing skills, rewrites them with the SkillOpt
  reflective optimizer, and A/Bs the result on held-out tasks, leaving a reviewable proposal.

The component map is in [docs/how-it-works.md](docs/how-it-works.md); deeper design in
[ARCHITECTURE.md](ARCHITECTURE.md).

## Documentation

| Doc | Contents |
|-----|----------|
| [Tutorial](docs/tutorial.md) | The full loop end to end: route, mine, generate, review, promote, roll back |
| [How it works](docs/how-it-works.md) | Component map (MCP server, agent, optimizer, UI) |
| [Configuration](docs/configuration.md) | Env reference, candidate generation, cross-model compatibility, eval task sets, Langfuse |
| [The evidence gate](docs/evidence-gate.md) | The anti reward-hacking checks a reviewer relies on |
| [Privacy & security](docs/security.md) | Zero-data-retention, network exposure, threat model |
| [Sign in with Google (SSO)](docs/sso.md) | Domain-restricted login and roles for a shared deployment |
| [Bring your own agent](docs/mcp-integration.md) | Use the MCP server from your own harness; tracing |
| [Skill sources](docs/skill-sources.md) | Where `scripts/fetch_skills.sh` gets skills, and their licenses |

## License

MIT, see [LICENSE](LICENSE).
