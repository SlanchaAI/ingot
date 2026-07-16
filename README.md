# Ingot

**Mine your traffic. Refine your skills.**

<p align="center">
  <img src="docs/ingot.jpg" alt="Ingot, the mascot, handing skills out to AI agents" width="720">
</p>

**Ingot** is a self-improving [Agent Skills](https://github.com/anthropics/skills) library. An **MCP server**
routes tasks to skills by embedding similarity and serves them to a **LangGraph deep agent**; every
run is traced to a self-hosted **[Langfuse](https://langfuse.com)**; a **[GEPA](https://github.com/gepa-ai/gepa)
optimizer** mines the traces for failures, rewrites a failing skill, and **A/B-tests champion vs
challenger through the full agent** on held-out tasks; you review the diff and scores in a small
**approval UI**, and the winner goes live via hot reload — no restart.

## Privacy first

**Privacy focused throughout** — three properties, all defaults, none optional:

- **Zero data retention LLM calls.** Every OpenRouter request — agent runs, GEPA rollouts and
  reflection, the judge, task drafting — carries a hardcoded provider preference:

  ```json
  {"provider": {"zdr": true, "data_collection": "deny"}}
  ```

  OpenRouter then routes only to **zero-data-retention endpoints** operated by providers that do
  not collect user data; a model with no qualifying endpoint fails loudly rather than falling back
  to one that retains prompts.
- **Self-hosted tracing.** Langfuse (and its Postgres / ClickHouse / MinIO) runs inside the compose
  stack — traces, skill contents, and eval outputs never leave your machine.
- **Localhost only.** No service is reachable off the machine
  (see [Network exposure](#network-exposure)).

The only data that leaves your machine is the LLM traffic itself, under ZDR.

## Tutorial

The tutorial runs the whole loop on a real skill, and the arc is simple: **find a request
the existing skill doesn't help with, then optimize the skill until it does.** The failing champion
is not a strawman: it is the **current `pdf` skill from
[anthropics/skills](https://github.com/anthropics/skills), exactly as fetched from upstream
today** — the agent routes to it, loads it, follows it, and still fails the request. You'll watch
that happen, mine the failure from your own traces, let GEPA rewrite the skill, review the diff,
promote it, and re-run the *same request* to see it succeed.

### 1. Set up and start the stack

```bash
git clone https://github.com/SlanchaAI/ingot.git && cd ingot
cp .env.example .env               # put your OpenRouter key in it (https://openrouter.ai/keys)
scripts/fetch_skills.sh all        # fetch ~70 real skills into ./skills (see Skill sources)
docker compose up --build
```

This brings up the MCP server (`localhost:8000`), Langfuse (`localhost:3100`), the approval UI
(`localhost:8080`) — and runs the agent once on a demo task so you have something to look at.

No skills are committed to this repo — `fetch_skills.sh` clones each source, copies its skills in,
and deletes the clone, so everything stays under its own upstream license. Without an OpenRouter
key everything still starts and the router still prints suggestions (the embedding router needs no
LLM); the agent and optimizer will tell you what to set and exit cleanly.

### 2. The agent routes to a skill and uses it

```bash
docker compose run --rm agent "How do I merge several PDFs into one and add page numbers?"
```

```
PROPOSED SKILLS (MCP suggest_skills):
    0.74  pdf — Use this skill whenever the user wants to do anything with PDF files...

LOADED SKILLS (MCP get_skill): ['pdf']
TOKENS: 23233 in / 698 out

RESULT:
... (working pypdf + reportlab code, following the loaded skill)
[agent] trace sent to Langfuse (http://localhost:3100)
```

The agent asked the router (`suggest_skills`), loaded the top match (`get_skill`), and followed it.
Open **http://localhost:3100** (login `demo@local.dev` / `localdemo123` — local demo literals baked
into the compose file, not secrets) to see the full trace: every tool call, LLM call, and token count.

### 3. Find a request the stock skill doesn't help with

Now ask for something the skill's own docs say it covers — rotating pages. (No `scan_mix.pdf`
exists, and none needs to: the request asks for a *script*, so the filename is just a placeholder
in the code you get back.) The router matches, the agent loads the skill and follows it — and the
loaded skill still doesn't get the user working code:

```bash
docker compose run --rm agent "Write Python code that straightens just the sideways (landscape) pages of scan_mix.pdf and saves a corrected copy."
```

```
PROPOSED SKILLS (MCP suggest_skills):
   0.642  pdf (related — compose/extend) — Use this skill whenever the user wants to do anything...

LOADED SKILLS (MCP get_skill): ['pdf']
TOKENS: 33721 in / 1241 out

RESULT:
Created `straighten_landscape.py`. Here's what it does:

1. Opens `scan_mix.pdf` with `pypdf`
2. For each page, checks if `width > height` (landscape)
3. Rotates landscape pages **-90°** to make them portrait; leaves portrait pages alone
4. Saves the result as `scan_mix_corrected.pdf`
...
```

Read that answer again: the user asked for code and got a *description* of code. The script exists
only in the agent's throwaway container filesystem — the user receives nothing runnable. Other
requests fare no better (a form-fill request came back with an empty answer after 75k input
tokens). This is the tutorial's premise: **loading today's Anthropic `pdf` skill did not help with
this request.** The failure is systematic and traces back to the skill itself — the stock SKILL.md
is a 7,500-character reference guide to browse, not an instruction contract, so the serving model
reads it and paraphrases instead of delivering. The rest of the tutorial optimizes the skill until
this exact request works.

### 4. Mine what's failing (from real traces)

After a few runs like the ones above, Langfuse holds your real traffic. `optimize-mine` re-judges
each logged outcome with a multi-dimensional LLM judge and aggregates which failure dimensions
dominate — the [SkillForge paper's](https://arxiv.org/abs/2604.08618) "Failure Analyzer" applied to
your own traces:

```bash
docker compose run --rm optimize-mine pdf
```

```
[mine] analyzed 6 real traces · mean judge score 0.42 · 3 bad cases (score < 0.5)
[mine] failure dimensions (paper's Failure Analyzer), most common first:
    completeness             4/6  ███████
        · 'Write a Python script that stamps a diagonal DRAFT watermark across ev' → no code provided
        · 'How do I extract all the text from statement.pdf in Python? It might b' → missing PIL import for OCR
    instruction_following    3/6  █████
        · 'Write a Python script that stamps a diagonal DRAFT watermark across ev' → no runnable python code block
        · 'Write Python code that straightens just the sideways (landscape) pages' → no runnable python code block
    correctness              2/6  ███
        · 'Write a Python script that stamps a diagonal DRAFT watermark across ev' → no code provided
    efficiency               0/6

[mine] 6 weakest tasks mined as eval candidates → optimize on these next.
```

The dominant failure — *no runnable code provided* — is exactly what you watched in step 3. That
categorized signal also feeds GEPA's reflection (*diagnose → smallest targeted fix*, not a blind
rewrite), so the optimization below is aimed, not scattershot.

### 5. Optimize the skill so it does help: GEPA + held-out A/B

```bash
docker compose run --rm optimize pdf --budget 30
```

One command does two things (~$0.50 of OpenRouter credit at budget 30, ~15–20 minutes; the default
budget is 60 metric calls — 30 is plenty for a task set this small):

1. **GEPA evolves the SKILL.md `body`** — the instructions the agent actually loads — on the
   *train* tasks in `optimize/tasks/pdf.yaml`, using judge critiques to author better versions.
   The routing `description` is deliberately **not** optimized here: it's an embedding-matched
   routing trigger, not instructions, and a quality judge can't measure routing (set
   `OPTIMIZE_COMPONENTS` to widen, including bundled `file:` components — those are diffed for
   review but never executed by the A/B, so keep scripts out unless you have execution-grounded
   evals). Everything else is preserved on disk as-is. A length penalty keeps GEPA from winning by
   bloat. (A skill without a task set gets one **auto-drafted** — train/holdout — by the teacher
   model first.)
2. **Champion vs challenger through the full agent** — real router, real tool calls — on the
   **held-out** tasks GEPA never saw, scored on quality and token cost. GEPA optimizes *against*
   the train judge score, so that score is biased by construction; promotion is gated on tasks the
   optimizer never touched.

<!-- FILL:OPTIMIZE-RUN -->

Output tokens are the cost that matters (they're generated on every future task); a challenger that
wins on quality but regresses output tokens >10% gets a ⚠ flag. A bigger SKILL.md (input tokens) is
cheap context by comparison.

**Optimization is greedy — one component per pass, each scored by its own role's metric:**

| pass | command | inner-loop objective | cost |
|------|---------|---------------------|------|
| body (default) | `optimize pdf` | LLM judge on train tasks; full-agent A/B gate | ~$1 |
| description | `optimize pdf --description` | the **routing suite**, scored by the real embedding router — no LLM rollouts (reflection only) | ~$0.05, minutes |
| scripts | `optimize pdf --scripts` | refused for now: bundled scripts need execution-grounded evals before a rewrite can be measured | — |

The description pass gates on **no regression on any routing metric, at least one strict
improvement, and no collision** with another skill's description — then the same human approval UI.
This split exists because a quality judge can't measure routing and a router can't measure quality;
one shared metric let early runs "improve" the description in ways that either broke routing (the
gate caught it) or never reached the serving agent.

### 6. Review and promote in the approval UI

Open **http://localhost:8080**:

![approval UI — skills list](docs/ui-home.png)

Click **Review** to see the judge scores, the token shift, and the SKILL.md diff:

![approval UI — pending challenger review](docs/ui-review.png)

**Approve & promote** verifies the evidence still matches the on-disk champion and the exact
challenger, snapshots the prior revision, and swaps the challenger into `skills/pdf/`. The MCP
server notices the revision change on its next request — the new skill is served with **no
restart**. **Reject** discards it.

### 7. The same request now works

The request from step 3 — the one the stock skill didn't help with — again, against the promoted
skill:

<!-- FILL:AFTER-RUN -->

That's the loop: a request the existing skill couldn't serve → mined failures → targeted rewrite →
held-out A/B → human review → hot-reloaded promotion → the same request served properly.

### 8. (Optional) Promote via a live canary instead

The offline A/B gates on a fixed held-out set. The production-honest alternative is a **canary**:
serve the challenger to a fraction of *live* traffic, judge each real outcome, and promote only
once the challenger's posterior beats the champion's:

```bash
docker compose run --rm optimize-canary pdf --epsilon 0.5
```

```
[canary] 'pdf': routing 50% of traffic to the challenger, judging each outcome (promote at P≥0.95, …)
[canary] req  4: challenger ok | served champ 2 / chall 2 | P(chall>champ)=0.50
[canary] req 21: challenger ok | served champ 13 / chall 8 | P(chall>champ)=0.89
[canary] req 23: champion   ·  | served champ 15 / chall 8 | P(chall>champ)=0.94
[canary] inconclusive after 24 requests (P=0.91) — keeping champion; raise --max/--epsilon for more samples.
```

Each request flips an ε-coin, the outcome is judged, each arm keeps a Beta posterior, and the run
stops early to promote (P≥0.95) or reject (P≤0.05). In this run the challenger climbed to P=0.91 —
likely better, but under the conservative bar — so the canary kept the champion rather than flip
production on borderline evidence. The gate is deliberately hard to trip. Add `--promote` to
auto-promote on a win; otherwise a win is recorded as a recommendation for the UI.

Every canary request is **first-class in Langfuse**: its trace is tagged with the arm and the exact
skill revision (`canary=challenger`, `revision=<hash>`), and the judged outcome is written back as
scores (`canary_judge`, `canary_success`) on that trace — so the per-arm evidence is auditable in
the Langfuse UI and the posterior can be recomputed from stored scores at any time.

### 9. (Optional) Put it on autopilot

One command mines every skill's real traffic for health and optimizes only the ones actually
failing, leaving every survivor in the approval UI (nothing auto-promotes):

```bash
docker compose run --rm optimize-loop            # all skills with eval sets; add names to target some
```

```
[loop] ===== pdf =====
[mine] analyzed 23 real traces · mean judge score 0.52 · 11 bad cases
[loop] pdf: below health bar (mean 0.52) — optimizing…
   … GEPA + held-out A/B + gate …
[loop] done. 1 challenger(s) passed the gate and are queued for review at http://localhost:8080: ['pdf']
```

Point it at your logged traffic on a schedule and skills that drift on your real workload get
re-optimized and queued for a human — continuously.

### 10. Grow the library

A skill is just a directory `skills/<name>/SKILL.md`: YAML frontmatter whose `description` is the
routing key, and a body the agent loads. Three ways the library grows:

**Write one yourself** — create the file and hot-reload (no rebuild; `skills/` is bind-mounted):

```bash
mkdir -p skills/sql-explain
cat > skills/sql-explain/SKILL.md <<'EOF'
---
name: sql-explain
description: Use this skill whenever the user wants to understand, optimize, or debug a SQL query —
  reading EXPLAIN/EXPLAIN ANALYZE output, spotting missing indexes, or rewriting a slow query.
---

# SQL query analysis
1. Run `EXPLAIN (ANALYZE, BUFFERS)` and read the plan bottom-up.
2. Flag sequential scans on large tables → candidate indexes.
... (your reusable method here)
EOF

docker compose restart mcp        # or call the reload_skills MCP tool for a live hot reload
```

Only two frontmatter fields matter: `name` (a slug) and `description` (the routing trigger — write
it "pushy", starting with "Use this skill when…", since under-triggering is the common failure).

**Let the agent write one** — when `suggest_skills` returns an empty list (nothing even related),
the agent solves the task itself and persists what it learned via the `create_skill` MCP tool:

```bash
docker compose run --rm agent "Plan a strict low-FODMAP weekly dinner menu for two people"
# PROPOSED SKILLS (MCP suggest_skills):            <- empty: no skill covers this
# ... solves the task ...
# mcp log: [ingot] created skill 'low-fodmap-meal-planning' — live immediately
```

**Compose instead of sprawl** — if a skill is merely *related* (similarity in a band below the
routing threshold), `suggest_skills` returns it flagged `related: true` and the agent is told to
load and **extend or compose** it rather than author a near-duplicate. Any created skill is
immediately optimizable: the optimizer auto-drafts an eval task set if none exists.

---

## Keeping the optimizer honest (anti reward-hacking)

Optimizing a skill against an **LLM judge** invites the classic failure mode: the challenger learns
to please the judge, not to actually get better. Guards close the obvious paths:

1. **Judge ≠ author.** GEPA's reflection LM (`GEPA_MODEL`) writes the skill; the judge
   (`JUDGE_MODEL`) is a *different* model — same-model author/grader would share blind spots, so
   the optimizer warns loudly if you configure that. Set `JUDGE_MODELS=a,b` for an ensemble judge
   (mean score, majority-vote on failure dimensions) — harder still to exploit.
2. **Held-out gate, not a lucky mean.** Promotion requires a **margin** (`PROMOTE_MIN_MARGIN`,
   default +0.15, set above the judge's noise floor), **enough samples** (`PROMOTE_MIN_SAMPLES`),
   and **no catastrophic per-task regression** (the challenger may not drop a held-out task the
   champion passed below the pass line).
3. **No routing hacks.** If GEPA rewrites the routing `description`, promotion re-checks it against
   every other skill's — a rewrite that over-broadly **shadows** another skill (cosine ≥
   `COLLISION_SCORE`) is blocked, so a skill can't grab traffic by widening its trigger.
4. **Execution-grounded judging.** For code tasks an objective check (`execcheck.py`) extracts and
   `ast.parse`s the code and hands the judge a verdict it must treat as ground truth — "described
   the code" or a syntax error can't be talked into a high score. Opt-in `EXEC_SANDBOX=1`
   additionally *runs* it in a subprocess (a missing-fixture error counts as inconclusive, not a
   defect). Despite the name this is a bare subprocess, **not** an isolated sandbox — only enable
   it inside the disposable `optimize` container, never on the host.
5. **Length penalty.** GEPA's objective subtracts a penalty for a bloated body, so it can't win by
   padding the skill with filler the judge mistakes for completeness.
6. **Deletions need evidence.** The reflection prompt tells GEPA not to remove guidance the observed
   failures don't implicate. If a challenger still drops most of the champion body (retention below
   `RETENTION_WARN`), the review UI shows a ⚠ warning with the retention number and the held-out
   sample count — a small eval can't quietly license a large deletion. (A warning, not a block: the
   tutorial's own rewrite is a legitimate wholesale fix, and the human reviews the diff either way.)
7. **Human override, informed.** A challenger that wins the mean but fails the gate is still
   recorded — the UI shows a red **⛔ promotion gate** banner with the reasons, so a human can
   override deliberately rather than rubber-stamp a gamed win.

```
[ab] champion 0.55 vs challenger 0.60 -> CHALLENGER WINS
[ab] ⛔ challenger won the mean but the promotion gate BLOCKED it:
     margin +0.10 < required +0.15; catastrophic regression on 1 task(s) the champion passed
```

## Configuration

Set in `.env` (never committed):

| var | default | notes |
|-----|---------|-------|
| `OPENROUTER_API_KEY` | — | required ([get one](https://openrouter.ai/keys)) |
| `MODEL` | `qwen/qwen3.6-27b` | the agent — everything that *executes* skills, incl. GEPA rollouts |
| `GEPA_MODEL` | `z-ai/glm-5.2` | GEPA's reflection LM (the skill author) |
| `JUDGE_MODEL` | `google/gemini-2.5-flash` | the LLM judge — must differ from `GEPA_MODEL` (anti reward-hacking) |
| `MIN_SCORE` | `0.65` | at/above → routable match; below → `related` band or novel |
| `RELATED_SCORE` | `0.45` | floor of the `related` (compose/extend) band below `MIN_SCORE` |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | router embedding model — keep in sync with the Dockerfile's `EMBED_MODEL` build arg |
| `BODY_TARGET_CHARS` | `6000` | GEPA's length penalty starts past this body size |
| `LENGTH_PENALTY` | `0.10` | max score subtracted for a very long body |
| `LOOP_HEALTH_THRESHOLD` | `0.7` | continuous loop re-optimizes skills whose mined mean score is below this |
| `RETENTION_WARN` | `0.5` | ⚠ review warning when the challenger keeps less than this fraction of the champion body |
| `OPTIMIZE_COMPONENTS` | `body` | what GEPA may rewrite; add `description` (routing gate applies) or `file:<path>` entries (diffed, never executed — avoid scripts) |
| `SKILL_MAX_DESCRIPTION` | `1024` | `create_skill` description hard cap (Agent Skills spec) |
| `SKILL_MAX_BODY` | `40000` | `create_skill` body ceiling (~500 lines) |

See [Keeping the optimizer honest](#keeping-the-optimizer-honest-anti-reward-hacking) for the
promotion-gate knobs (`PROMOTE_MIN_MARGIN`, `PROMOTE_MIN_SAMPLES`, `COLLISION_SCORE`, `JUDGE_MODELS`).

The teacher/student split is deliberate: a strong model authors and judges skills, but rollouts and
the A/B always run on the model the skills will actually serve.

## How it works

- **`mcp_server/`** — [FastMCP](https://github.com/jlowin/fastmcp) v3 server (HTTP transport), six tools:
  - `suggest_skills(task, k)` — routable matches by embedding similarity (CPU [fastembed](https://github.com/qdrant/fastembed), no GPU); if none, returns near-misses flagged `related` (compose-awareness); empty = truly novel. (`list_skills()` exists for debug/UI but is kept out of the agent's toolset — the agent routes, it doesn't scan.)
  - `get_skill(name)` — the full SKILL.md to load
  - `create_skill(name, description, body)` — persist a new agent-authored skill (never overwrites)
  - `reload_skills()` — hot reload after promotion/creation (or `docker compose restart mcp`)
  - `route_and_load(task, harness, cwd, available_tools, available_mcps)` — optional one-round-trip
    selection for external clients; returns one compatible skill body or no match
- **`agent/run.py`** — [deepagents](https://github.com/langchain-ai/deepagents) LangGraph agent wired to those tools via `langchain-mcp-adapters`, traced to Langfuse.
- **`skills/<name>/SKILL.md`** — YAML `description` is the routing key; the body is what the agent loads.
- **`optimize/`** — success/failure mining over real traces (`mine.py`), multi-dimensional LLM judge (`judge.py`), GEPA loop over the skill description/body with diagnose→minimal-edit reflection (`gepa_loop.py`), A/B + revisioned evidence (`ab.py`), live **canary** promotion (`canary.py`), snapshot/staged promotion with rollback (`promote.py`), per-role token ledger (`usage.py`). A/B agents get mutation tools stripped, so evals can't alter the library. Promotion records the exact skill revisions plus `evidence.json`/`EVIDENCE.md`, and refuses stale or mismatched revisions. The mining + categorized-failure ideas are borrowed from [SkillForge (Liu et al., arXiv:2604.08618)](https://arxiv.org/abs/2604.08618).
- **`ui/`** — FastAPI approval UI (one HTML page, no build step).

### Optional shared skill roots

The Docker demo reads and writes `skills/`. To route across additional libraries, set
`SKILL_ROUTER_PATHS` to a platform-separated list of directories:

```bash
export SKILL_ROUTER_PATHS="$HOME/Source/team-skills:$HOME/.agents/skills"
docker compose up --build
```

The local `skills/` authoring root is searched first, followed by configured roots; the first
duplicate name wins with a warning. Optional `metadata.skill-router` frontmatter can restrict
automatic matches by harness, project path, platform, required tools/MCPs, trust, activation mode,
priority, and conflicts.

## Skill sources

**No skills are committed** — every source is optional and pulled from upstream by
`scripts/fetch_skills.sh` (clone → copy skill dirs → delete the clone), so each stays under its own
license and nothing is redistributed here. All are curated from the
[VoltAgent index](https://github.com/VoltAgent/awesome-agent-skills):

| source arg | repo | skills | license |
|------------|------|--------|---------|
| `anthropics` | [anthropics/skills](https://github.com/anthropics/skills) | document skills (pdf, docx, pptx, xlsx, …) | per-skill (see frontmatter) |
| `nvidia` | [nvidia/skills](https://github.com/nvidia/skills) | GPU / infra / data / medical imaging | Apache-2.0 |
| `lambdatest` | [LambdaTest/agent-skills](https://github.com/LambdaTest/agent-skills) | testing frameworks (pytest, playwright, cypress, appium, …) | MIT |
| `trailofbits` | [trailofbits/skills](https://github.com/trailofbits/skills) | security analysis (semgrep, static analysis, vuln scanners, …) | CC-BY-SA-4.0 |

```bash
scripts/fetch_skills.sh all                    # everything above (whole dirs, incl. bundled files)
scripts/fetch_skills.sh anthropics trailofbits # or pick sources
docker compose restart mcp                      # pick up the new skills
```

Fetching skips skills already present (never clobbers) and caps large sources. Trail of Bits' skills
are **CC-BY-SA-4.0** (share-alike); LambdaTest is **MIT**; nvidia is **Apache-2.0**; Anthropic's carry
per-skill licenses in their frontmatter — review each source's terms before redistributing.

## Security & threat model

**A loaded skill is instructions the agent follows.** That's the whole mechanism — so treat skill
content as *code*, and the skills library as *trusted state that must be curated*. You cannot fully
"solve" prompt injection in a system whose job is to retrieve and follow instructions; the design
goal is proportionate guardrails plus a small, well-defended write surface.

Write paths, and what guards each:

- **`create_skill` (agent-authored, goes live with no human approval)** — the highest-risk path, so
  it's the most guarded: slug + frontmatter sanitization (`yaml.safe_dump`), never overwrites an
  existing skill, Agent-Skills-spec name/description limits (≤64-char slug, ≤1024-char description,
  no reserved words, no XML tags), an instruction-override / prompt-injection phrase check
  (`mcp_server/safety.py`), an embedding **collision check** that rejects a description which
  near-duplicates an existing skill's (blocks *route-shadowing* / memory-poisoning), and an
  **optional ML classifier** (below). Accepted skills are tagged `source: agent` in frontmatter.
- **GEPA promotion** — content is authored by the reflection LM but **gated by human approval**: it
  lands in `runs/pending/` and you review the per-component diff in the UI before it goes live.
- **Third-party skills** (`skills/` from nvidia/anthropic) — unaudited but not attacker-controlled at
  runtime; review them as you would any dependency.

### Optional: ML prompt-injection classifier

Beyond the regex heuristic, `create_skill` can run the **[vLLM Semantic Router](https://github.com/vllm-project/semantic-router)
jailbreak detector** ([`llm-semantic-router/mmbert32k-jailbreak-detector-merged`](https://huggingface.co/llm-semantic-router/mmbert32k-jailbreak-detector-merged),
an mmBERT CPU classifier, Apache-2.0). Inference runs on **ONNX Runtime** via the model repo's
bundled ONNX export — no `torch`/`transformers`; the deps already ship with the base image via
fastembed. It's **opt-in**, and like the skills, the model is **not redistributed here** — it
downloads (~1.2GB, one-time) from Hugging Face into the HF cache on first use, under its own
upstream license. Just point the guard at the model:

```bash
# point the guard at the model (empty = disabled)
export SKILL_GUARD_MODEL=llm-semantic-router/mmbert32k-jailbreak-detector-merged
# optional: export SKILL_GUARD_THRESHOLD=0.7   (the classifier's default)
# optional: export SKILL_GUARD_ONNX_FILE=onnx/model.onnx   (which export to load)
```

With it set, every `create_skill` call is scored and a jailbreak/injection classification above the
threshold is rejected alongside the regex check (~20ms per call on CPU). When the model is missing
or the download fails it **degrades silently** to the regex heuristic — no crash. To run it in
Docker, set `SKILL_GUARD_MODEL` on the `mcp` service in `docker-compose.yml` (mount a persistent
`HF_HOME` to keep the one-time download).

### Network exposure

**Everything is localhost-only by default, because nothing is authenticated.** The MCP tools
(including the mutating `create_skill` / `reload_skills`) and the approval UI's endpoints (which can
trigger paid optimization runs and promote skills) have no auth of their own — the demo's protection
is that no service is reachable off the machine:

- `docker-compose.yml` publishes every port on loopback only (`127.0.0.1:8000` MCP,
  `127.0.0.1:8080` UI, `127.0.0.1:3100` Langfuse).
- Run directly (outside Docker), the MCP server also binds `127.0.0.1` by default; the compose file
  sets `HOST=0.0.0.0` *inside* the container, where it's still shielded by the loopback publish.

To make a service reachable from other machines on your network, change its port mapping in
`docker-compose.yml` from `"127.0.0.1:8000:8000"` to `"8000:8000"` (or bind a specific interface,
e.g. `"192.168.1.20:8000:8000"`) — same for the UI's `8080`. Do this knowingly: anyone who can reach
those ports can create skills, promote challengers, and start optimization runs that spend your
OpenRouter budget. For anything beyond a trusted LAN, put an authenticating reverse proxy in front.

What is deliberately **not** done (and why): we do **not** denylist shell commands, `.env`/credential
mentions, or `curl … | sh` in skill bodies — legitimate skills routinely contain code, install steps,
and secret-handling guidance, so scanning for those produces constant false positives. The residual
risk is contained operationally instead: **run the agent in a container without real secrets or
sensitive host paths** (this demo's `agent` service mounts nothing sensitive and needs only
`OPENROUTER_API_KEY`). For a real deployment, add per-tool sandboxing and treat `create_skill` output
as untrusted until reviewed. Further reading:
[OpenAI on prompt injection](https://openai.com/safety/prompt-injections/).
