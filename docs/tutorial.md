# Tutorial

The tutorial takes one skill through the whole lifecycle: write the quick first-draft skill you'd
actually jot down, watch it under-deliver on real traffic, diagnose it, generate a candidate change,
then review, promote, and (if you want it back) roll back. Every command and number below comes
from a real run.

### 1. Set up and start the stack

```bash
git clone https://github.com/SlanchaAI/ingot.git && cd ingot
cp .env.example .env               # put your OpenRouter key in it (https://openrouter.ai/settings/keys)
scripts/fetch_skills.sh all        # fetch ~70 real skills into ./skills (see Skill sources)
docker compose up --build
```

This brings up the MCP server (`localhost:8000`) and the change-control UI (`localhost:8080`), then
runs the agent once on a demo task. The UI lists every skill the router serves, each with its
content-hash revision and a load count (how often it has actually been served), and surfaces
anything awaiting review:

![Change-control UI, the skills library, with revisions and load counts](ui-home.png)

No skills are committed to this repo. `fetch_skills.sh` clones each source, copies its skills in,
and deletes the clone, so everything stays under its own upstream license. Without an API key
everything still starts and the router still prints suggestions; the agent and the candidate
generator tell you what to set and exit cleanly.

### 2. The agent routes to a skill and uses it

```bash
docker compose run --rm agent "How do I merge several PDFs into one and add page numbers?"
```

```
COMPATIBLE ROUTE (MCP route_and_load):
    0.74  pdf: Use this skill whenever the user wants to do anything with PDF files...

SERVING MODEL: accounts/fireworks/models/qwen3p7-plus

LOADED SKILLS (MCP route_and_load): ['pdf@83a75cf1f9b5…']
TOKENS: 23233 in / 698 out

RESULT:
... (working pypdf + reportlab code, following the loaded skill)
```

The agent asked the canonical router (`route_and_load`), received one compatible skill body, and
followed it. The `@…` suffix is the skill's content-hash revision, which also lands on the trace as
a tag. Unconstrained suggestions never control the serving model or loaded instructions.
A routed task runs on the cheap `AGENT_MODEL` because the skill carries the method; only truly
novel tasks escalate to `STRONG_MODEL` (step 10). Steps 2 to 4 were recorded on Fireworks model
IDs and steps 5 to 8 on the OpenRouter defaults (`qwen/qwen3-32b` serving); the `SERVING MODEL`
line always shows whatever `AGENT_MODEL` you configure. The run's full trace just landed in
`runs/traces.jsonl`; that local store is what the miner reads in step 4.

### 3. Write a first-draft skill and watch it under-deliver

A skill is a directory with a `SKILL.md`: YAML frontmatter whose `description` is the routing key,
and a body the agent loads. The tutorial skill is **Tailwind CSS**, a library with a hard version
break (v4 moved configuration from `tailwind.config.js` into CSS), so the quick notes you'd jot
down are not just thin, they're stale:

```bash
mkdir -p skills/tailwind
cat > skills/tailwind/SKILL.md <<'EOF'
---
name: tailwind
description: Use this skill when the user needs help with Tailwind CSS.
---

# Tailwind CSS

1. Install with npm and run the init command to create tailwind.config.js.
2. Add your source files to the `content` array so classes are picked up.
3. Put custom colors, fonts, and breakpoints under `theme.extend`.
4. Start your CSS with `@tailwind base;` `@tailwind components;` `@tailwind utilities;`.
EOF
```

Every line of that body is the v3 way, wrong since v4. `skills/` is bind-mounted and the server
hot-reloads on change, so the skill is live immediately. Send it some realistic traffic:

```bash
for t in "Add a brand color to my Tailwind setup so bg-brand works." \
         "Set up Tailwind in a fresh Vite app." \
         "Why aren't the classes from my component library in node_modules getting styled?"; do
  docker compose run --rm agent "$t"
done
```

```
== Add a brand color to my Tailwind setup so bg-brand works.
   0.723  tailwind - Use this skill when the user needs help with Tailwind CSS.
LOADED SKILLS (MCP get_skill): (none)
== Set up Tailwind in a fresh Vite app.
   0.709  tailwind - Use this skill when the user needs help with Tailwind CSS.
LOADED SKILLS (MCP get_skill): ['tailwind@77156f5efe1a…']
== Why aren't the classes from my component library in node_modules getting styled?
   0.621  web-artifacts-builder (related - compose/extend) - Suite of tools for creating elaborate…
LOADED SKILLS (MCP get_skill): (none)
```

Three requests, three different failures. The requests that say "Tailwind" route fine, and the
second one actually loaded the skill, serving the stale v3 notes to live traffic. The third
request never says "Tailwind" and misrouted to `web-artifacts-builder`, which mentions Tailwind in
its description. All three failures are now sitting in your traces.

### 4. Mine what's failing (from real traces)

`optimize-mine` re-judges your logged traffic with a multi-dimensional LLM judge (the
[SkillForge paper's](https://arxiv.org/abs/2604.08618) "Failure Analyzer" applied to your own
traces). A trace counts toward a skill if it's tagged with it or ranks it in the embedding top-5,
so misrouted traffic still counts toward the skill that should have served it:

```bash
docker compose run --rm optimize-mine tailwind
```

```
[mine] 39/50 recent traces relevant to 'tailwind'
[mine] analyzed 39 real traces · mean judge score 0.50 · 20 bad cases (score < 0.5)
[mine] failure dimensions, most common first:
    correctness             20/39  █████
    completeness            20/39  █████
    instruction_following   20/39  █████
    efficiency              16/39  ████
[mine] 6 weakest tasks mined as eval candidates → optimize on these next.
```

The diagnosis is version drift: answers built on `tailwind.config.js` and `@tailwind` directives,
the v3 world the stub teaches. The weakest mined tasks are surfaced as eval candidates for
`optimize/tasks/<skill>.yaml`; see [Writing eval task sets](configuration.md#writing-eval-task-sets).

Reference-free judging of live traffic is noisy. Treat mined dimensions as a diagnosis to
investigate, not a verdict; the evidence gate runs on rubrics.

### 5. (Optional) Generate a candidate in the background

At this point you know what is wrong and could fix the body by hand: edit `SKILL.md`, and the router
serves the new revision on the next request. This step does it the other way, with the optional
candidate generator, so the change arrives with measured evidence attached.

Write an eval task set for the skill (`optimize/tasks/tailwind.yaml`) with train and holdout tasks
whose rubrics carry the v4 ground truth (the teacher can also auto-draft one on a skill's first CLI
run). Then run it headless, which is how it is meant to run:

```bash
docker compose run --rm optimize tailwind
```

The generator authors several candidate bodies in parallel, races them on the train tasks, and A/Bs
the winner against the champion through the full agent on the held-out tasks (about two minutes, a
few cents). The UI's **Generate candidate** button starts the same run. Our run:

```
[skillopt] seed: hard 0.000 soft 0.000 gate 0.000 (mixed) on 6 train tasks; 2 epoch(s), minibatch 3, ≤3 edits/step
[skillopt] step 1: accept_new_best (1 edit(s)), gate 0.750
[skillopt] step 2: accept_new_best (3 edit(s)), gate 0.767
[skillopt] step 3: accept_new_best (2 edit(s)), gate 0.808
[skillopt] step 4: accept_new_best (1 edit(s)), gate 0.817
[skillopt] winner after 4 step(s): gate 0.817 (seed 0.000)
[ab] champion: mean judge score 0.133  [0.0, 0.2, 0.2, 0.0, 0.2, 0.2]
[ab] challenger: mean judge score 0.667  [0.8, 1.0, 0.9, 0.9, 0.4, 0.0]
[ab] champion 0.133 vs challenger 0.667 -> CHALLENGER WINS
[ab] output tokens/task: 1278 -> 808 (-470)
[ab] ⚠ acceptance criteria (minority, flagged for review): 'no_v3_tailwind_directives': 1/6 holdout answer(s) matched
[ab] ⚠ challenger drops 80% of the champion body, gated on only 6 held-out task(s), review the deletions carefully
  estimated cost: $0.06 (OpenRouter list prices)
[ab] pending approval written to /app/runs/pending/tailwind.json - review + promote at http://localhost:8080
```

Read what happened. Every line of the stub body is the v3 way, so it scored 0.000 in bare rollouts.
SkillOpt reflected on the failing minibatches, and because the judge feedback flagged the loaded
guidance as *wrong* (not just incomplete), it proposed bounded **delete/replace** edits that stripped
the stale v3 lines rather than appending beside them, that is the 80% body drop the review gate
flags. Across four accepted steps the body became clean v4. On the held-out A/B through the real
agent, the stub champion scored 0.133, a small serving model follows the loaded v3 advice straight
into wrong answers, while the challenger scored **0.667 with fewer tokens** (808 vs 1278 out/task).
The acceptance gate caught a residual: on 1 of 6 holdout tasks the weak model still emitted a v3
directive despite the clean body, a minority, so the graded gate flags it as a ⚠ warning rather
than blocking. The result is **promotable-but-flagged**: a human weighs the large deletion and the
residual slip in the comparison panel before approving, and only then does `skills/tailwind` change.

Note what the run did *not* do: it did not touch `skills/tailwind/`. It wrote a quarantined record
and an evidence bundle, and stopped.

The size of the win tracks the serving model: **body-pass wins concentrate where the body carries
knowledge the serving model doesn't have** (weak or older models, internal tools, your
conventions, post-cutoff dependencies). On a frontier serving model the same experiment ends the
other way: in an earlier recorded run the champion scored a perfect 1.000 (the strong model
recognized the v3 stub as stale and answered v4 from its own knowledge) and the gate refused the
rewrite. The gate's refusals are what make the wins trustworthy: in a companion run against the
NVIDIA-authored `accelerated-computing-cudf` skill, a challenger that dropped half the champion
body and regressed a held-out task was blocked, 0.980 vs 0.800.

Generation is greedy: one component per pass, each scored by its own role's metric:

| pass | command | candidate-search objective | cost |
|------|---------|---------------------------|------|
| body (default) | `optimize tailwind`, or the UI's **Generate candidate** button | LLM judge on train tasks; full-agent A/B for the evidence | ~$0.05 |
| description | `optimize tailwind --description` | the routing suite, scored by the real embedding router; no LLM rollouts | ~$0.01, a couple of minutes |
| scripts | `optimize tailwind --scripts` | LLM judge grounded by execution checks; greedy, one bundled `scripts/` file at a time | like the body pass, per file |

The split exists because a quality judge can't measure routing and a router can't measure quality.
The scripts pass refuses to run until the skill's holdout has at least one execution-grounded
`check:` entry, because the judge alone can't tell a broken script from a working one; with checks
in place, both the rollouts and the A/B serve the assembled skill (body plus bundled files), so a
rewritten file is actually exercised by the evidence run. Other bundled text files can still join
the body pass by name (`OPTIMIZE_COMPONENTS=body,file:<path>`). The candidate search serves each
rollout under the exact contract the A/B serves, so the search can't optimize against different
instructions than the A/B measures. `GEPA_ROLLOUTS=agent` runs every rollout through the full agent
scaffold instead (a legacy variable name: it predates the removal of the GEPA body loop and now
selects the SkillOpt candidate search's rollout mode).

### 6. Review, promote, roll back

Back at **http://localhost:8080**, the header pill flips to **1 to review**, the Review section
leads with the evidence, and the `tailwind` row shows a `change awaiting review` chip. The card
carries the champion-vs-challenger judge scores, the before/after token shift, the gate verdict and
its warnings, the recorded evidence bundle, and the component diff, here the red lines are the v3
guidance SkillOpt removed and the green lines are the v4 body it wrote:

![Review card, the tailwind v3→v4 challenger, promotable with warnings](ui-review.png)

**Approve** doesn't promote in one click: it opens a comparison panel with the model breakdown,
the before/after token usage, and the per-task judge scores, and a final **Approve & promote** that
reveals a separate **Confirm**, so a promotion is deliberate.

![Comparison panel, model, tokens, and judge scores before Confirm](ui-compare.png)

**Approve & promote** verifies the evidence still matches the on-disk champion, snapshots the prior
revision into `runs/revisions/tailwind/<revision>/`, swaps the challenger into `skills/tailwind/` by
rename, and appends an `approve` record to `runs/approval-audit.jsonl`. The MCP server picks up the
revision change with no restart. **Reject** discards the candidate.

That snapshot is the undo. It appears in the UI's **History** section, and restoring it is one
click, or one command:

```bash
# --entrypoint python replaces the service's own `python -m optimize.ab` entrypoint
docker compose run --rm --entrypoint python optimize -m optimize.promote rollback tailwind <revision>
```

Rollback snapshots the revision it displaces too, so the round trip is symmetric, and it writes its
own audit record. The trail records the actor as `local-operator` for every action: the local UI
has no identity or authentication, so it can record that a local operator approved, not who. Both
records are appended after the swap has already happened, so an audit write that fails (a full or
read-only disk) is logged and the change stands: a missing line means the trail is incomplete, not
that the promotion was rolled back.

One review slot exists per skill: promote or reject before running a different pass, or the
displaced candidate is archived beside the slot (the run tells you where) rather than reviewed.

### 7. Fix the routing with the description pass

The body is fixed, but step 3's third request still misroutes: the routing key is the
`description`, so routing gets its own pass with its own metric, run against the `routing:` cases
in `optimize/tasks/tailwind.yaml`: realistic positive phrasings plus `expected: null` negatives.
The cases that matter are the real misses, so put your mined traffic in the suite (we added the
node_modules request verbatim, plus a "classes disappear in the production build" variant):

```bash
docker compose run --rm optimize tailwind --description
```

```
[routing] inner-loop score: seed 0.286 -> best 0.714
[routing] champion: top1 0.500 · recall@3 0.500 · no-route precision 0.000
[routing] challenger: top1 1.000 · recall@3 1.000 · no-route precision 0.333
[routing] pending description written to /app/runs/pending/tailwind.json - review + promote at http://localhost:8080
[usage] tokens spent by this routing pass (reflection only):
  reflection      7 calls      3,796 in     7,154 out
  estimated cost: $0.01 (OpenRouter list prices)
```

Top-1 routing goes 0.500 to 1.000 for a cent of reflection calls. Every candidate description is
scored by the real embedding router against the real skill corpus, so there's nothing for an LLM
judge to be fooled about. The winning description names the symptoms users actually type
("missing styles from component libraries in node_modules") and learned explicit negatives from
the `expected: null` cases ("Do not use this skill for plain CSS, vanilla CSS grid/flexbox
styling"), which is where the no-route precision gain comes from. The gate requires no regression
on any routing metric, at least one strict improvement, and no collision with another skill's
description. The pass is stochastic: a run can land a weaker (still gate-passing) challenger, and
re-running the same pass overwrites the slot in place.

### 8. The same request now finds the skill

Promote the routing challenger in the UI exactly as in step 6, then replay the miss:

```bash
docker compose run --rm agent "Why aren't the classes from my component library in node_modules getting styled?"
```

```
PROPOSED SKILLS (MCP suggest_skills):
   0.667  tailwind - Use this skill when the user needs help with Tailwind CSS, including fra…
SERVING MODEL: qwen/qwen3-32b
LOADED SKILLS (MCP get_skill): (none)
```

The exact request that misrouted in step 3 now routes to `tailwind` top-1. One honest caveat,
visible right there in the output: on this request the serving model answered without loading the
routed skill at all. Live loading behavior belongs to the serving model; the controlled
body-vs-body comparison is the step 5 A/B, which injects the body and guarantees serving. Routing
puts the right skill in front of the model; the A/B proves what happens when it is actually used.

That's the lifecycle: a first-draft skill → real traffic → mined diagnosis → a proposed change with
held-out evidence → human approval → hot reload → a snapshot you can roll back to.

### 9. (Optional) Run candidate generation unattended

This is where candidate generation belongs: in the background, not on the review path. One command mines
every skill's real traffic for health and proposes changes only for the ones actually failing,
leaving every survivor quarantined in the review queue (nothing auto-promotes):

```bash
docker compose run --rm optimize-loop            # all skills with eval sets; add names to target some
```

```
[loop] ===== pdf =====
[mine] analyzed 23 real traces · mean judge score 0.52 · 11 bad cases
[loop] pdf: below health bar (mean 0.52), optimizing…
[loop] done. 1 challenger(s) queued for review: ['pdf']
```

Routing quality decays as the library *grows* (a new skill's description can shadow an old one),
so there is also a library-wide routing health check: it replays every skill's routing suite
against the real router plus a description-collision scan, embedding-only, no LLM, no key. It
exits non-zero on problems, so it slots into cron or CI:

```bash
docker compose run --rm --entrypoint "python -m optimize.routing_health" optimize
# [health] tailwind: top1 1.000 · recall@3 1.000 · no-route precision 0.333 (7 cases)
# [health] ✓ routing healthy: every suite passes and no descriptions collide.
```

### 10. Grow the library

Two ways the library grows:

- **Write one yourself**, exactly like step 3. Only two frontmatter fields matter: `name` (a slug)
  and `description` (the routing trigger; write it "pushy", starting with "Use this skill
  when…", since under-triggering is the common failure). A truly novel request (empty
  `suggest_skills`) is served by `STRONG_MODEL` in the meantime; how often that fires is governed
  by `RELATED_SCORE`.
- **Compose instead of sprawl.** If a skill is merely related (similarity below the routing
  threshold), `suggest_skills` returns it flagged `related: true` and the agent is told to extend
  or compose it rather than author a near-duplicate.

---

