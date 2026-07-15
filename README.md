# MCP Skill Router

A minimal, end-to-end example of a **self-improving skill library**:

- an **MCP server** that suggests and serves [Agent Skills](https://github.com/anthropics/skills) (fetch 70+ real ones — document, GPU/infra, testing, security — from four public sources, see [Skill sources](#skill-sources)),
- a **LangGraph deep agent** that asks the router for suggestions, loads the best skill, follows it — and **authors a new skill** when nothing matches,
- **[Langfuse](https://langfuse.com)** (self-hosted, included in compose) tracing every run,
- a **[GEPA](https://github.com/gepa-ai/gepa) optimizer** that evolves a skill's SKILL.md, **A/B-tests champion vs challenger through the full agent** (judge score + token cost), and
- a tiny **approval UI**: review the diff and scores, approve, and the winner goes live via a **hot reload** of the MCP server — no restart.

## The payoff, in one story

You ask the agent to **fill out a PDF form**. It routes to the stock `pdf` skill (a reference guide)
and the answer is subtly wrong — it forgets `NeedAppearances`, so the filled fields render blank.
Annoying, and *systematic*: mining your Langfuse traces shows the same skill fails **16/23** real PDF
requests on correctness, mostly *"described the code instead of writing it"* and missing exactly these
details.

So you run one command. **GEPA** re-writes the skill against those mined failures — from a browse-y
guide into an explicit contract (*read the request → pick the library by fixed rules → output complete,
runnable code*) — and A/B-tests it through the full agent on **held-out** PDF operations it never
trained on: judge score **0.05 → 0.525** (10×), output tokens **−30%**. You eyeball the diff in the UI,
click approve, and it's live with no restart.

Now a *different* form-filling request comes in — and the agent returns a concise, complete script that
sets the checkbox to `/Yes` and the fields actually render. The library got better at your workload,
you approved the one change, and the next user just… got a working answer.

Every step below is real and reproducible — `docker compose up` and follow along.

## Quick start

```bash
cp .env.example .env               # put your OpenRouter key in it
scripts/fetch_skills.sh all        # fetch example skills into ./skills (see Skill sources)
docker compose up --build
```

No skills are committed to this repo — `fetch_skills.sh` clones each source, copies its skills in, and
deletes the clone, so everything stays under its own upstream license (pass `anthropics`, `nvidia`,
`lambdatest`, `trailofbits`, or `all`). Then `docker compose up` brings up the MCP server, Langfuse,
the approval UI, and runs the agent once on a demo task.

---

## The full loop, step by step

### 1. The agent routes to a skill and uses it

```bash
docker compose run --rm agent "Merge a.pdf and b.pdf into one PDF and add page numbers"
```

```
PROPOSED SKILLS (MCP suggest_skills):
    0.73  pdf — Use this skill whenever the user wants to do anything with PDF files...
   0.615  pptx — Use this skill any time a .pptx file is involved in any way...
   0.601  docx — Use this skill whenever the user wants to create, read, edit...

LOADED SKILLS (MCP get_skill): ['pdf']
TOKENS: 20843 in / 1551 out

RESULT:
... (working pypdf + reportlab code, following the loaded skill)
[agent] trace sent to Langfuse (http://localhost:3100)
```

### 2. Every run is traced in Langfuse

Open **http://localhost:3100** (login `demo@local.dev` / `localdemo123` — headless-init demo
credentials baked into the compose file; local literals, not secrets). Each run is a trace with
every tool call, LLM call, and token count.

### 3. Create and load a new skill

A skill is just a directory `skills/<name>/SKILL.md` with YAML frontmatter — the `description` is the
routing key, the body is what the agent loads. There are two ways to add one.

**a) Write it yourself.** Create the file and hot-reload (no restart):

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

# tell the running server to pick up the new skill (skills/ is bind-mounted, so no rebuild):
docker compose restart mcp        # or call the reload_skills MCP tool for a live hot reload
```

The router embeds the new `description` on reload. A matching task now routes to it:

```bash
docker compose run --rm agent "Why is this Postgres query doing a seq scan and how do I speed it up?"
# PROPOSED SKILLS:  0.71  sql-explain — Use this skill whenever the user wants to understand...
# LOADED SKILLS: ['sql-explain']
```

Only two frontmatter fields matter: `name` (must be a slug: lowercase, digits, dashes) and
`description` (the routing trigger — write it "pushy", starting with "Use this skill when…", since
under-triggering is the common failure). A skill with no `description` is skipped by the router.

**b) Let the agent write it.** When `suggest_skills` returns an **empty list** (every match below
`MIN_SCORE`, default 0.65), the agent solves the task itself and persists what it learned via the
`create_skill` MCP tool — the library grows itself:

```bash
docker compose run --rm agent "Plan a strict low-FODMAP weekly dinner menu for two people"
# PROPOSED SKILLS (MCP suggest_skills):            <- empty: no skill covers this
# ... solves the task ...
# mcp log: [skill-router] created skill 'low-fodmap-meal-planning' — live immediately

docker compose run --rm agent "What can I cook this week? I have IBS and need a gentle diet"
# PROPOSED SKILLS:
#    0.687  low-fodmap-meal-planning — Use this skill when planning low-FODMAP meals...
# LOADED SKILLS: ['low-fodmap-meal-planning']      <- routed to the skill it just wrote
```

`create_skill` never overwrites an existing skill and sanitizes the name/frontmatter. To then
*improve* a created skill, mine what's failing (next) and run the optimizer — which **auto-drafts an
eval task set** (train/holdout) with the teacher model if the skill doesn't have one yet, so any
freshly created skill is immediately optimizable.

**Compose-awareness.** The agent doesn't blindly create when there's no *exact* match. If a skill is
merely *related* (similarity in a band below the routing threshold), `suggest_skills` returns it
flagged `related: true`, and the agent is told to **load and extend/compose** it rather than author a
near-duplicate — so the library grows by reuse, not sprawl:

```
docker compose run --rm agent "summarize a news article about tennis"
# PROPOSED SKILLS:
#    0.57  template-skill (related — compose/extend) — ...
#    0.57  pptx (related — compose/extend) — ...
```

### 4. Mine what's failing (from real traces)

Every agent run is already in Langfuse, so before optimizing you can ask *what's actually going
wrong*. `optimize/mine.py` re-judges real logged outcomes and classifies each failure across fixed
dimensions — the [SkillForge paper's](https://arxiv.org/abs/2604.08618) "Failure Analyzer" applied to
your own traffic:

```bash
docker compose run --rm optimize-mine pdf
```

```
[mine] analyzed 23 real traces · mean judge score 0.52 · 11 bad cases (score < 0.5)
[mine] failure dimensions (paper's Failure Analyzer), most common first:
    correctness             16/23  ███████
        · 'rotates only the landscape pages of mixed.pdf…' → Conceptually right but no actual code provided
    completeness            15/23  ███████
        · 'fill out the form fields in application.pdf…'   → Missing NeedAppearances flag for rendering
    instruction_following   13/23  ██████
        · 'rotates only the landscape pages of mixed.pdf…' → Described code instead of writing complete runnable code
    efficiency              13/23  ██████
        · 'fill out the form fields in application.pdf…'   → Included unrequested PDF creation script
[mine] 6 weakest tasks mined as eval candidates → optimize on these next.
```

The top failure — **instruction-following: "described code instead of writing runnable code"** — is
exactly the weakness the optimizer fixes below. That categorized signal also feeds GEPA's reflection
(*diagnose → smallest targeted fix*, not a blind rewrite), so optimization is aimed, not scattershot.

### 5. Optimize a skill: GEPA + A/B eval

```bash
docker compose run --rm optimize pdf
```

GEPA evolves the skill's **routing `description` + SKILL.md `body`** — the two things the agent
actually loads and the A/B actually measures — on the **train** tasks in `optimize/tasks/pdf.yaml`.
(Bundled files — `reference.md`, `scripts/*.py`, `LICENSE` — are *preserved as-is*, not optimized: a
text optimizer shouldn't rewrite a license or unrun code, and the A/B doesn't execute them. Optimizing
those is future work, see [What's next](#whats-next).) A **length penalty** on the body keeps GEPA
from winning by bloat. An LLM judge scores each rollout **and writes a critique**, and a reflection
LM uses those critiques to author better versions. The task set is
split **train / holdout**: GEPA only ever sees train, and the A/B + promotion decision (below) is
judged on the **held-out** tasks — different PDF operations the optimizer never touched. This isn't
benchmark hygiene (there's no leaderboard to game) — it's that GEPA *optimizes against* the train
judge score, so that score is a biased estimate by construction. Gating promotion on data the
optimizer didn't push on is how you avoid shipping a skill that just learned to satisfy the judge on
a few phrasings. **In production the honest form of this is temporal or online** — optimize on older
traces, gate on a recent slice or a live canary / ε-exploration A/B — and the static offline split
here is a cheap stand-in for that. (A task set with only a flat `tasks:` list skips the split and
leans on the human approval gate + live monitoring instead.)

```
[gepa] optimizing 'pdf' (2 components) on 4 train tasks (budget 60 metric calls)…
[gepa] inner-loop score: seed 0.938 -> best 1.000     # on the TRAIN tasks GEPA optimizes
[gepa] components changed: ['body']                   # e.g. ['description', 'body'] if it rewrote both
[gepa] challenger checkpointed to runs/challenger-pdf.json (resume with --challenger-file)
```

> A/B faithfully measures the evolved **description + body** (what `get_skill` serves). Changes to
> bundled files are shown in the review diff and written on promotion, but aren't executed during the
> A/B (the eval agent reads files from the on-disk champion) — review those by eye.

Then champion vs challenger run **through the full deep agent** (real router, real tool calls) on the
**held-out** tasks (form-fill, rotate, encrypt, extract-images — none of which GEPA trained on), each
as a Langfuse dataset run — side-by-side in the UI — scored on quality *and* token cost:

```
[ab] champion:   mean judge score 0.050   [0.0, 0.2, 0.0, 0.0]
[ab] challenger: mean judge score 0.525   [0.2, 1.0, 0.7, 0.2]
[ab] champion 0.050 vs challenger 0.525 -> CHALLENGER WINS
[ab] output tokens/task: 1072 -> 750 (-322)      # -30%
[ab] input tokens/task:  41857 -> 26032 (-15825) (informational)
[usage] tokens spent by this optimization run:
  rollout        60 calls    875,682 in   105,129 out
  judge          68 calls     40,238 in    26,684 out
  reflection      2 calls      5,461 in     3,638 out
  agent_ab        8 calls    271,555 in     7,286 out
  TOTAL         138 calls  1,192,936 in   142,737 out      (~$1 at current OpenRouter prices)
[ab] pending approval written to runs/pending/pdf.json — review + promote at http://localhost:8080
```

Output tokens are treated as the cost that matters (they're generated on every future task); a
challenger that wins on quality but regresses output tokens >10% gets a ⚠ flag. A bigger SKILL.md
(input tokens) is cheap context by comparison.

**What actually improved — and read these numbers honestly.** GEPA nails the *train* tasks (0.94 →
1.00), but the number that gates promotion is the **held-out 0.05 → 0.525** — a 10× lift on PDF
operations the optimizer never saw, at −30% output tokens. The champion's near-zero is the exact
failure the mining step surfaced: through the full agent, the reference-guide champion *described*
code instead of writing it (the top `instruction_following` failure). The challenger rewrites the
skill around an explicit contract — *read the request → pick the library by fixed rules → output a
concise, fully runnable script* — and generalizes it to unseen operations. Note the challenger is
**0.525, not 0.9**: it genuinely improved but is far from perfect on operations it never trained on —
which is what an honest held-out number looks like, and exactly why promotion stays human-gated.

### 6. Review and promote in the approval UI

Open **http://localhost:8080**:

![approval UI — skills list](docs/ui-home.png)

Click **Review** to see judge scores, the token shift, and the SKILL.md diff:

![approval UI — pending challenger review](docs/ui-review.png)

**Approve & promote** requires passing evidence for the current champion and exact challenger,
snapshots the prior revision, then swaps the staged skill into `skills/pdf/`. The MCP server notices
the revision change on its next request, so the new version is served with no restart. Reject
discards the challenger.

To optimize another skill, just run `optimize <skill>` — if it has no task set, the teacher
auto-drafts a train/holdout one first.

### 7. (Optional) Promote via a live canary instead

The offline A/B gates on a fixed held-out set. The production-honest alternative is a **canary**:
serve the challenger to a fraction of *live* traffic, judge each real outcome, and promote only once
the challenger's posterior beats the champion's — protecting live traffic and gating on real results.

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

Each request flips an ε-coin (champion vs challenger), the outcome is judged, and each arm keeps a
Beta posterior; after each request we estimate `P(challenger > champion)` and stop early to promote
(≥0.95) or reject (≤0.05). Here the challenger climbed to **P=0.91** — likely better — but stayed
under the conservative 0.95 bar, so the canary **kept the champion and would keep sampling** rather
than flip production on borderline evidence. (It also shows the champion isn't as weak on the full
live mix as its holdout-only 0.05 implied.) That's the point: the gate is deliberately hard to trip.
Add `--promote` to auto-promote on a win; otherwise a win is recorded as a recommendation for the UI.

### 8. Put it on autopilot (the continuous loop)

The pieces above compose into one command: **mine every skill's real traffic for health, and optimize
only the ones that are actually failing** — auto-drafting an eval set where none exists, and leaving
every survivor in the approval UI (nothing auto-promotes).

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

This is the production shape: point it at your logged traffic on a schedule, and skills that drift on
your real workload get re-optimized and queued for a human — the strong-model teacher improving the
served model's skills, continuously.

---

## Keeping the optimizer honest (anti reward-hacking)

Optimizing a skill against an **LLM judge** invites the classic failure mode: the challenger learns to
please the judge, not to actually get better. Four guards close the obvious paths:

1. **Judge ≠ author.** GEPA's reflection LM (`GEPA_MODEL`, GLM) writes the skill; the judge
   (`JUDGE_MODEL`, default **`google/gemini-2.5-flash`**) is a *different* model. If the author and
   grader were the same model they'd share blind spots and GEPA could game them — so the optimizer
   warns loudly if you set them to the same model. Set `JUDGE_MODELS=a,b` for an **ensemble** judge
   (mean score, majority-vote on failure dimensions) — harder still to exploit.
2. **Held-out gate, not a lucky mean.** A challenger that merely wins the average can be exploiting a
   small/noisy eval. Promotion additionally requires a **margin** (`PROMOTE_MIN_MARGIN`, default
   +0.15, set above the judge's noise floor), **enough samples** (`PROMOTE_MIN_SAMPLES`), and **no catastrophic per-task regression**
   (it may not drop a held-out task the champion passed below the pass line).
3. **No routing hacks.** If GEPA rewrites the *routing description*, promotion re-checks it against
   every other skill's description — a rewrite that over-broadly **shadows** another skill (cosine ≥
   `COLLISION_SCORE`) is blocked, so a skill can't grab traffic by widening its trigger.
4. **Execution-grounded judging.** For code tasks the judge doesn't just *read* the code — an
   objective check (`execcheck.py`) extracts and `ast.parse`s it, and hands the judge a verdict it's
   told to treat as ground truth, so "described the code" or a syntax error can't be talked into a
   high score. Opt-in `EXEC_SANDBOX=1` additionally *runs* it in a subprocess (a missing-fixture
   error is treated as inconclusive, not a code defect).
5. **Length penalty.** GEPA's objective subtracts a penalty for a bloated body, so it can't win by
   padding the skill with filler the judge mistakes for completeness.
6. **Human override, informed.** A challenger that wins the mean but fails the gate is still recorded
   for review — the UI shows a red **⛔ promotion gate** banner with the reasons, so a human can
   override deliberately rather than rubber-stamp a gamed win.

```
[ab] champion 0.55 vs challenger 0.60 -> CHALLENGER WINS
[ab] ⛔ challenger won the mean but the promotion gate BLOCKED it:
     margin +0.10 < required +0.15; catastrophic regression on 1 task(s) the champion passed
```

Still not done (documented, not built): holdout rotation to prevent multiple-comparisons leakage
across repeated runs, a fixed never-regress suite, and true sandboxed execution *with fixtures* to
optimize the bundled `scripts/`. See [What's next](#whats-next).

---

## Configuration

Set in `.env` (never committed):

| var | default | notes |
|-----|---------|-------|
| `OPENROUTER_API_KEY` | — | required ([get one](https://openrouter.ai/keys)) |
| `MODEL` | `qwen/qwen3.6-27b` | the agent — everything that *executes* skills, incl. GEPA rollouts |
| `GEPA_MODEL` | `z-ai/glm-5.2` | GEPA's reflection LM (the skill author) |
| `JUDGE_MODEL` | `google/gemini-2.5-flash` | the LLM judge — must differ from `GEPA_MODEL` (anti reward-hacking) |
| `MIN_SCORE` | `0.65` | at/above → routable match; below → `related` band or novel |

See [Keeping the optimizer honest](#keeping-the-optimizer-honest-anti-reward-hacking) for the
promotion-gate knobs (`PROMOTE_MIN_MARGIN`, `PROMOTE_MIN_SAMPLES`, `COLLISION_SCORE`, `JUDGE_MODELS`).

The teacher/student split is deliberate: a strong model authors and judges skills, but rollouts and
the A/B always run on the model the skills will actually serve. Without a key, the demo still prints
the router's **suggestions** (the embedding router needs no LLM).

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
- **`optimize/`** — success/failure mining over real traces (`mine.py`), multi-dimensional LLM judge (`judge.py`), GEPA loop over the skill description/body with diagnose→minimal-edit reflection (`gepa_loop.py`), A/B + revisioned evidence (`ab.py`), live **canary** promotion (`canary.py`), snapshot/staged promotion (`promote.py`), per-role token ledger (`usage.py`). A/B agents get mutation tools stripped, so evals can't alter the library. The mining + categorized-failure ideas are borrowed from [SkillForge (Liu et al., arXiv:2604.08618)](https://arxiv.org/abs/2604.08618).
- **`ui/`** — FastAPI approval UI (one HTML page, no build step).

### Optional shared skill roots

The Docker demo still reads and writes `skills/`. To route across additional libraries, set
`SKILL_ROUTER_PATHS` to a platform-separated list of directories:

```bash
export SKILL_ROUTER_PATHS="$HOME/Source/team-skills:$HOME/.agents/skills"
docker compose up --build
```

The local `skills/` authoring root is searched first, followed by configured roots. The first
duplicate name wins with a warning. This keeps `create_skill` and optimization compatible while letting another
MCP client call `route_and_load` against shared skills. Optional `metadata.skill-router` frontmatter
can restrict automatic matches by harness, project path, platform, required tools/MCPs, trust,
activation mode, priority, and conflicts.

### Behavioral promotion evidence

The optimizer still performs the existing champion/challenger A/B workflow. Its promotion gate now
also requires a real holdout split, checks routing regressions when descriptions change, records the
exact complete skill revisions, and emits `evidence.json` plus `EVIDENCE.md`. Promotion refuses stale
or mismatched revisions, snapshots the live skill, stages the challenger, and rolls back a failed
swap. Behavioral checks strengthen the improvement loop; they do not replace it.

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
jailbreak detector** (`llm-semantic-router/mmbert32k-jailbreak-detector-merged`, an mmBERT CPU
classifier). It's **opt-in** — its `transformers`+`torch` deps are kept out of the base image so
`docker compose up` stays light. Enable it:

```bash
# 1. install the extra deps (into the mcp image, or a derived one)
pip install -r requirements-guard.txt
# 2. point the guard at the model (empty = disabled)
export SKILL_GUARD_MODEL=llm-semantic-router/mmbert32k-jailbreak-detector-merged
# optional: export SKILL_GUARD_THRESHOLD=0.7   (the classifier's default)
```

With it set, every `create_skill` call is scored and a jailbreak/injection classification above the
threshold is rejected alongside the regex check. When the deps or model are missing it **degrades
silently** to the regex heuristic — no crash. To run it in Docker, add the two lines to `requirements.txt`
(or build a derived image) and set `SKILL_GUARD_MODEL` on the `mcp` service in `docker-compose.yml`.

What is deliberately **not** done (and why): we do **not** denylist shell commands, `.env`/credential
mentions, or `curl … | sh` in skill bodies — legitimate skills routinely contain code, install steps,
and secret-handling guidance, so scanning for those produces constant false positives. The residual
risk is contained operationally instead: **run the agent in a container without real secrets or
sensitive host paths** (this demo's `agent` service mounts nothing sensitive and needs only
`OPENROUTER_API_KEY`). For a real deployment, add per-tool sandboxing and treat `create_skill` output
as untrusted until reviewed. Further reading:
[OpenAI on prompt injection](https://openai.com/safety/prompt-injections/).

## Tests

**155 fast unit tests** (no network/LLM — mocked) in `tests/` cover the security guards (content scan,
name/traversal validation, YAML-injection round-trip, collision, the ML classifier's decision logic),
the optimizer (promotion gate, judge parsing/clamping + ensemble aggregation, canary Thompson decision,
length penalty, execution-based code check + sandbox, task drafting, continuous-loop health-gating,
token-ledger thread-safety), the router (retrieval, thresholds, `nearest`, routing-eval metrics),
the registry (multi-root precedence, revisions, harness variants), and promotion (behavioral
evidence, snapshot/staged swap + rollback, server-side revision refresh). The suite is hermetic —
it passes with or without fetched skills in `skills/`:

```bash
docker run --rm -v $(pwd):/app -w /app skill-router-mcp python -m pytest tests -q
```

One **opt-in integration test** exercises the real jailbreak classifier and auto-skips unless the
model is present — to run it, build an image with the guard deps and set the model:

```bash
docker build -t skill-router-guard - <<'DOCKER'
FROM skill-router-mcp
RUN pip install --no-cache-dir "transformers>=4.44" torch --index-url https://download.pytorch.org/whl/cpu
DOCKER
docker run --rm -e SKILL_GUARD_MODEL=llm-semantic-router/mmbert32k-jailbreak-detector-merged \
  -e HF_HOME=/app/.hf_cache -v $(pwd):/app -w /app skill-router-guard python -m pytest tests -q
```

## What's next

- **Optimize the bundled files, honestly.** Today GEPA only touches the routing description + SKILL.md
  body (what the A/B measures); `reference.md` / `scripts/*.py` are preserved as-is. Optimizing scripts
  needs true sandboxed execution *with fixtures* so a rewrite can be measured, not guessed.
- **Deeper execution-based scoring.** `EXEC_SANDBOX=1` runs code today; next is per-task fixtures and
  assertions so "does it actually produce the right output" becomes the score, not an LLM's read of it.
- **Overfitting hardening.** Rotate the holdout across repeated runs (multiple-comparisons leakage), and
  keep a fixed never-regress suite separate from the optimize task set.
- **Trace → skill attribution.** The loop mines recent traffic; scoping traces to the *right* skill by
  Langfuse tag/cluster makes per-skill health sharper at volume.

## Honest footnotes

- **Judge variance is real** even at temperature 0 (the same seed skill has scored anywhere from
  0.6 to 0.94 across runs). Read small A/B deltas skeptically; the held-out gap above (0.05 → 0.525,
  a 10× lift) is large enough to trust, but the exact figures move run to run.
- **The holdout is small and offline** (a handful of tasks) — enough to keep the promotion gate off
  the metric GEPA optimized, but a real deployment would gate on a recent/live traffic slice (canary),
  not a static split, and use more of it for a tighter estimate.
- **Langfuse shows token counts out of the box**; for $ cost, add your models' prices under
  Project Settings → Models.
