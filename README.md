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
  self-hosted) if you'd rather not run the bundled one.
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
docker compose up                  # lite by default: skill router (:8000) + change-control UI (:8080)
                                   #   + one demo agent run
docker compose run --rm agent "How do I merge several PDFs into one and add page numbers?"
```

The change-control UI at `localhost:8080` asks for a login; the compose default is **`admin` /
`ingot`**. Change `AUTH_PASSWORD` in `.env` before sharing it on your LAN (or set it empty to run
open), see [Privacy & security](docs/security.md#network-exposure).

`docker compose up` brings up a self-hosted Langfuse (traces + experiment UI) alongside the router
and UI; trace mining reads from it and has no local fallback, so it fails loudly if no
Langfuse-compatible backend is reachable. Point `LANGFUSE_*` at your own Langfuse (Cloud or
self-hosted) to skip the bundled one. Backend, model, and gate settings live in
[Configuration](docs/configuration.md).

Then walk the full loop, a stale Tailwind skill mined, rewritten, gated, and promoted, in the
[**Tutorial**](docs/tutorial.md).

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
