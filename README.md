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

The only data that leaves your machine is the LLM traffic itself, under ZDR — and the endpoint is
yours to choose: `BASE_URL` + `API_KEY` point everything at **any OpenAI-compatible provider**
(`MODEL_BASE_URL`/`MODEL_API_KEY` override just the serving role for hybrid setups):

```bash
# provider-direct, e.g. Fireworks (zero data retention per Fireworks' serverless policy):
BASE_URL=https://api.fireworks.ai/inference/v1
API_KEY=fw_...
MODEL=accounts/fireworks/models/qwen3p7-plus
GEPA_MODEL=accounts/fireworks/models/glm-5p2
JUDGE_MODEL=accounts/fireworks/models/deepseek-v4-pro

# fully local (no key needed at all): everything on Ollama / vLLM
BASE_URL=http://172.17.0.1:11434/v1  MODEL=qwen3:32b  GEPA_MODEL=qwen3:32b  JUDGE_MODEL=llama3.3:70b
```

The hardcoded ZDR provider preference applies to OpenRouter endpoints; provider-direct endpoints
get a clean OpenAI-compatible request under that vendor's own retention policy, and local
endpoints are the strongest privacy of all. No API key is required when nothing points at a hosted
endpoint. From inside the compose containers, "localhost" is the container — use your host's LAN
IP (or `172.17.0.1` on Linux).

## Tutorial

The tutorial runs the whole improvement loop on a skill you write yourself, and the arc is the
product's honest pitch: **write the quick first-draft skill you'd actually jot down, watch it
under-deliver on real traffic, and let the system mature it** — content and routing, each measured
by its own metric, each change gated and human-approved. Every command, number, and screenshot
below comes from a real run; nothing is mocked.

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

SERVING MODEL: qwen/qwen3.6-27b

LOADED SKILLS (MCP get_skill): ['pdf']
TOKENS: 23233 in / 698 out

RESULT:
... (working pypdf + reportlab code, following the loaded skill)
[agent] trace sent to Langfuse (http://localhost:3100)
```

The agent asked the router (`suggest_skills`), loaded the top match (`get_skill`), and followed it.
The `SERVING MODEL` line is the weak/strong split at work: a routed task runs on the cheap `MODEL`
because the skill carries the method — only truly novel tasks escalate to `STRONG_MODEL` (step 11).
Open **http://localhost:3100** (login `demo@local.dev` / `localdemo123` — local demo literals baked
into the compose file, not secrets) to see the full trace: every tool call, LLM call, and token count.

### 3. Write a first-draft skill — and watch it under-deliver

A skill is a directory with a `SKILL.md`: YAML frontmatter whose `description` is the routing key,
and a body the agent loads. Write the quick version you'd actually jot down:

```bash
mkdir -p skills/excel-formulas
cat > skills/excel-formulas/SKILL.md <<'EOF'
---
name: excel-formulas
description: Use this skill when the user needs help writing or debugging spreadsheet formulas
  in Excel or Google Sheets — lookups, sums, conditionals, text manipulation, or date math.
---

# Excel formulas

1. Prefer built-in functions over manual arithmetic.
2. Use VLOOKUP to find values in tables.
3. Use IF for conditions; SUM and AVERAGE for aggregation.
4. Wrap formulas that can error in IFERROR.
EOF
```

`skills/` is bind-mounted and the server hot-reloads on change — the skill is live immediately.
Now send it some realistic traffic:

```bash
docker compose run --rm agent "For each order in A2:D500, look up the customer tier in Sheet2 and return the matching discount rate — orders with no match should show 0 instead of an error."
```

```
PROPOSED SKILLS (MCP suggest_skills):
    0.62  api-rate-limiting-helper (related — compose/extend) — Designs rate limiting strategies...

SERVING MODEL: qwen/qwen3.6-27b

LOADED SKILLS (MCP get_skill): (none)
TOKENS: 46428 in / 3512 out
```

Read that routing line again: a spreadsheet-lookup request matched **a rate-limiting skill**, and
`excel-formulas` never even surfaced — the first-draft description under-triggers (in our traffic,
3 of 4 realistic requests missed it). The answer itself came from the model's own knowledge after
burning 46k input tokens looking for spreadsheet files, resting on invented column assumptions.
The skill helped with none of it. Both failures are now sitting in your traces.

### 4. Mine what's failing (from real traces)

`optimize-mine` re-judges your logged traffic with a multi-dimensional LLM judge and aggregates
which failure dimensions dominate — the [SkillForge paper's](https://arxiv.org/abs/2604.08618)
"Failure Analyzer" applied to your own traces:

```bash
docker compose run --rm optimize-mine excel-formulas
```

```
[mine] analyzed 6 real traces · mean judge score 0.73 · 1 bad cases (score < 0.5)
[mine] failure dimensions (paper's Failure Analyzer), most common first:
    completeness             2/6  ███
        · 'For each order in A2:D500, look up the customer tier in Sheet2 and ret' → Python script not provided
        · 'For each order row in A2:D500, look up the customer tier from the tabl' → assumes customer ID column
    instruction_following    2/6  ███
        · 'For each order in A2:D500, look up the customer tier in Sheet2 and ret' → Python script not provided
    ...
[mine] 6 weakest tasks mined as eval candidates → optimize on these next.
```

(Reference-free judging of live traffic is noisier than rubric-based judging — treat mined
dimensions as a diagnosis to investigate, not a verdict. The optimizer's own gate runs on rubrics.)

### 5. Optimize the body: GEPA + held-out A/B

```bash
docker compose run --rm optimize excel-formulas
```

The skill has no eval set yet, so the teacher model **auto-drafts one** (train/holdout, persisted
to `optimize/tasks/excel-formulas.yaml`), then GEPA evolves the body on the train tasks and the
champion and challenger are A/B-ed through the full agent on the held-out tasks (~$1–1.5 of
OpenRouter credit; the serving-side A/B injects each variant body, so the comparison is exactly
body vs body):

```
[draft] no eval set for 'excel-formulas' — teacher (z-ai/glm-5.2) drafting 8 train/holdout tasks…
[gepa] optimizing 'excel-formulas' (components: ['body']; frozen: ['description']) on 4 train tasks…
[gepa] inner-loop score: seed 0.250 -> best 0.675
[gepa] components changed: ['body']
[ab] champion:   mean judge score 0.100  [0.4, 0.0, 0.0, 0.0]
[ab] challenger: mean judge score 0.675  [0.5, 0.7, 0.5, 1.0]
[ab] champion 0.100 vs challenger 0.675 -> CHALLENGER WINS
[ab] output tokens/task: 128 -> 160 (+32)  ⚠ output-token regression
[ab] ⚠ challenger drops 100% of the champion body, gated on only 4 held-out task(s) — review the deletions carefully
[ab] pending approval written to runs/pending/excel-formulas.json — review + promote at http://localhost:8080
```

The four-line stub scored **0.100** on held-out tasks; the challenger scores **0.675** — a +0.575
margin, far above the gate's +0.15 bar. Note both ⚠ flags are doing their jobs: output tokens grew
(richer answers than a stub's — worth a look, not a block), and the retention warning fires because
the challenger replaced the entire body — for a four-line stub that's exactly right, and the human
reviewing the diff decides.

**Optimization is greedy — one component per pass, each scored by its own role's metric:**

| pass | command | inner-loop objective | cost |
|------|---------|---------------------|------|
| body (default) | `optimize excel-formulas` | LLM judge on train tasks; full-agent A/B gate | ~$1 |
| description | `optimize excel-formulas --description` | the **routing suite**, scored by the real embedding router — no LLM rollouts (reflection only) | ~$0.05, seconds |
| scripts | `optimize excel-formulas --scripts` | refused for now: bundled scripts need execution-grounded evals before a rewrite can be measured | — |

This split exists because a quality judge can't measure routing and a router can't measure quality;
letting one metric grade both components teaches the optimizer to hide behavioral rules in the
routing description (the routing-regression gate catches it, but better to make it impossible).
The body pass's rollouts serve each candidate under the **exact contract the A/B serves**, so the
inner loop can't optimize against different instructions than the outer loop measures — and
`GEPA_ROLLOUTS=agent` runs every rollout through the full agent scaffold when the failures you're
chasing live there (e.g. code written to a scratch file instead of the answer).

### 6. Review and promote in the approval UI

Open **http://localhost:8080**:

![approval UI — skills list](docs/ui-home.png)

Click **Review** to see the judge scores, the token shift, both warnings, and the full body diff:

![approval UI — pending challenger review](docs/ui-review.png)

**Approve & promote** verifies the evidence still matches the on-disk champion and the exact
challenger, snapshots the prior revision, and swaps the challenger into `skills/excel-formulas/`.
The MCP server notices the revision change on its next request — the new body is served with **no
restart**. **Reject** discards it.

### 7. Fix the routing with the description pass

The body is better — but the router still under-triggers (step 3's lookup request never matched).
That's a *routing* problem, so it gets the routing-objective pass. It optimizes against the
`routing:` cases in `optimize/tasks/excel-formulas.yaml` — hand-write them (realistic positive
phrasings plus `expected: null` negatives), or let the teacher **auto-draft and persist** a suite
on first run:

```bash
docker compose run --rm optimize excel-formulas --description
```

```
[routing] optimizing 'excel-formulas' description against 6 routing cases (budget 40 metric calls;
          inner loop is embedding-only — no LLM rollouts)…
[routing] inner-loop score: seed 0.417 -> best 0.833
[routing] champion:   top1 0.250 · recall@3 0.500 · no-route precision 0.500
[routing] challenger: top1 1.000 · recall@3 1.000 · no-route precision 0.500
[routing] pending description written to runs/pending/excel-formulas.json — review + promote at http://localhost:8080
[usage] tokens spent by this routing pass (reflection only):
  reflection      4 calls      2,372 in     8,579 out
```

Top-1 routing goes **0.250 → 1.000** in seconds, for four reflection calls (~$0.03) — every
candidate description is scored by the real embedding router against the real skill corpus, so
there's nothing for an LLM judge to be fooled about. The gate requires no regression on any routing
metric, at least one strict improvement, and no collision with another skill's description; then
the same review UI shows the metric deltas and the description diff. Approve it.

### 8. The same request now finds the skill

```bash
docker compose run --rm agent "Write a formula that extracts the domain name from an email address in cell C2."
```

```
PROPOSED SKILLS (MCP suggest_skills):
   0.697  excel-formulas — Use this skill when the user asks to write, generate, or create an Excel...

RESULT:
=RIGHT(C2, LEN(C2) - FIND("@", C2))

This finds the `@` symbol and returns everything after it.
```

Requests that missed the skill or landed in the related band now route to it directly (top-1 went
1.000 on the routing suite), and the matured body wins its held-out A/B 0.675 to 0.100. One honest
caveat from our runs: on trivially easy requests the serving model sometimes answers without
bothering to load the matched skill at all — the controlled body-vs-body comparison is the A/B in
step 5, which guarantees serving; live loading behavior is the serving model's own.

That's the loop: a first-draft skill → real traffic → mined diagnosis → a body pass gated on
held-out quality → a description pass gated on routing metrics → human approval at every promotion
→ hot reload.

### 9. (Optional) Promote via a live canary instead

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

### 10. (Optional) Put it on autopilot

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

### 11. Grow the library

Three ways the library grows:

**Write one yourself** — exactly like step 3: a directory, a `SKILL.md`, and it's live on the next
request. Only two frontmatter fields matter: `name` (a slug) and `description` (the routing
trigger — write it "pushy", starting with "Use this skill when…", since under-triggering is the
common failure; and as step 7 showed, the description pass can fix it for you afterwards).

**Let the agent write one** — when `suggest_skills` returns an empty list (nothing even related),
the request escalates to `STRONG_MODEL` (defaults to `GEPA_MODEL`, the same teacher that rewrites
skills offline): it solves the task and persists what it learned via the `create_skill` MCP tool.
The new skill is distilled from a strong solution, and the next similar request routes to it on the
cheap `MODEL`:

```bash
docker compose run --rm agent "Plan a strict low-FODMAP weekly dinner menu for two people"
# PROPOSED SKILLS (MCP suggest_skills):            <- empty: no skill covers this
# SERVING MODEL: z-ai/glm-5.2 (strong — no skill matched, will author one)
# ... solves the task ...
# mcp log: [ingot] created skill 'low-fodmap-meal-planning' — live immediately
```

How often this fires is governed by `RELATED_SCORE`: the nearest-skill similarity floor rises with
library size (with ~70 skills even unrelated tasks score ≈0.5 against their closest neighbor), so
raise it if truly novel tasks keep landing in the related band instead of escalating.

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
   defect). And a task can go all the way to **artifact-verified execution** by shipping a
   `check:` spec in its task YAML — the answer's code then runs in a scratch directory seeded by
   the fixture, and the verdict is whether the assertion holds on what it produced, not what a
   judge thinks of the prose:

   ```yaml
   - task: "Write a Python script that uppercases input.txt into output.txt."
     rubric: "Complete runnable code, reads input.txt, writes output.txt."
     check:
       fixture: |
         open("input.txt", "w").write("hello")
       assert: |
         assert open("output.txt").read() == "HELLO"
   ```

   A broken fixture or missing dependency counts as inconclusive — the harness's failure is never
   held against the answer. All of these are bare subprocesses, **not** isolated sandboxes — only
   enable execution inside the disposable `optimize` container, never on the host.
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
| `BASE_URL` | `https://openrouter.ai/api/v1` | endpoint for everything — any OpenAI-compatible provider (Fireworks/Together direct, local vLLM/Ollama); `OPENROUTER_BASE_URL` is the legacy alias |
| `API_KEY` | — | bearer token for `BASE_URL`; `OPENROUTER_API_KEY` is the legacy alias. Local `http://` endpoints need no key |
| `MODEL_BASE_URL` / `MODEL_API_KEY` | `BASE_URL` / `API_KEY` | serving-role-only overrides (agent runs, A/B agents, GEPA rollouts) for hybrid setups |
| `OPENROUTER_PROVIDERS` | — | optional provider allowlist (e.g. `fireworks,deepinfra` → `provider.only`) — composes with ZDR, trades pool resilience for vendor predictability; pin/model conflicts are caught at startup with the list of providers that do serve each model |
| `GEPA_MODEL` | `z-ai/glm-5.2` | GEPA's reflection LM (the skill author) |
| `STRONG_MODEL` | `GEPA_MODEL` | serves novel requests: when the router finds no skill at all, the agent runs on this model instead of `MODEL` (weak/strong split at serving time), solves the task, and authors the new skill — so persisted skills are distilled from a strong solution. Uses the `BASE_URL` endpoint |
| `JUDGE_MODEL` | `google/gemini-2.5-flash` | the LLM judge — must differ from `GEPA_MODEL` (anti reward-hacking) |
| `MIN_SCORE` | `0.65` | at/above → routable match; below → `related` band or novel |
| `RELATED_SCORE` | `0.45` | floor of the `related` (compose/extend) band below `MIN_SCORE`; below it a task is *novel* (weak/strong escalation) |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | router embedding model — keep in sync with the Dockerfile's `EMBED_MODEL` build arg |
| `BODY_TARGET_CHARS` | `6000` | GEPA's length penalty starts past this body size |
| `LENGTH_PENALTY` | `0.10` | max score subtracted for a very long body |
| `LOOP_HEALTH_THRESHOLD` | `0.7` | continuous loop re-optimizes skills whose mined mean score is below this |
| `LOOP_PASSES` | `body` | passes the loop runs per unhealthy skill, in order (e.g. `body,description`) |
| `GEPA_ROLLOUTS` | `direct` | body-pass rollout mode: `direct` (one call under the serving contract) or `agent` (full scaffold per rollout — sees scaffold-driven failures, ~10× cost) |
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
    selection for external clients; returns one compatible skill body or no match, plus a `novel`
    flag — the weak/strong escalation signal (see [Bring your own agent](#bring-your-own-agent-mcp-only))
- **`agent/run.py`** — [deepagents](https://github.com/langchain-ai/deepagents) LangGraph agent wired to those tools via `langchain-mcp-adapters`, traced to Langfuse. Serves routed tasks on the weak `MODEL` and escalates truly novel tasks (empty `suggest_skills`) to `STRONG_MODEL`, which authors the new skill.
- **`skills/<name>/SKILL.md`** — YAML `description` is the routing key; the body is what the agent loads.
- **`optimize/`** — success/failure mining over real traces (`mine.py`), multi-dimensional LLM judge (`judge.py`), GEPA loop over the skill description/body with diagnose→minimal-edit reflection (`gepa_loop.py`), A/B + revisioned evidence (`ab.py`), live **canary** promotion (`canary.py`), snapshot/staged promotion with rollback (`promote.py`), per-role token ledger (`usage.py`). A/B agents get mutation tools stripped, so evals can't alter the library. Promotion records the exact skill revisions plus `evidence.json`/`EVIDENCE.md`, and refuses stale or mismatched revisions. The mining + categorized-failure ideas are borrowed from [SkillForge (Liu et al., arXiv:2604.08618)](https://arxiv.org/abs/2604.08618).
- **`ui/`** — FastAPI approval UI (one HTML page, no build step).

### Bring your own agent (MCP only)

Most deployments use just the MCP server with their own harness (Claude Code, Codex, a custom
agent) — the bundled `agent/run.py` is a reference client, not a requirement. Point your harness at
`http://localhost:8000/mcp` and call `route_and_load` once per request; its result is a three-way
branch:

- **`match`** — follow `skill_body`; a weak/cheap model suffices, the skill carries the method.
- **no match, `novel: false`** — related skills exist: call `suggest_skills` and compose or extend
  the closest instead of authoring a duplicate.
- **`novel: true`** — nothing even related. Serve the request with your strong model and have it
  persist its solution as a reusable skill via `create_skill` — the library grows exactly where
  routing failed, and the next similar request routes to the new skill on the weak model.

That three-way branch is the weak/strong serving split; the bundled agent implements the same
policy with `MODEL` (weak) and `STRONG_MODEL` (strong, defaults to `GEPA_MODEL`).

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
