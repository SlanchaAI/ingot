# Configuration

Set in `.env` (never committed):

| var | default | notes |
|-----|---------|-------|
| `BASE_URL` | `https://openrouter.ai/api/v1` | endpoint for everything; any OpenAI-compatible provider. On OpenRouter, [ZDR provider routing](https://openrouter.ai/docs/features/zdr) is enforced in code. `OPENROUTER_BASE_URL` is the legacy alias |
| `API_KEY` | (none) | bearer token for `BASE_URL`; `OPENROUTER_API_KEY` is the legacy alias. Local `http://` endpoints need no key |
| `AGENT_MODEL` | `qwen/qwen3.6-27b` | the agent: everything that executes skills, incl. rollouts; `MODEL` is the legacy alias |
| `MODEL_BASE_URL` / `MODEL_API_KEY` | `BASE_URL` / `API_KEY` | serving-role-only overrides for hybrid setups |
| `OPENROUTER_PROVIDERS` | (none) | OpenRouter only: provider priority (e.g. `fireworks,groq`), tried in order; composes with ZDR, and roles no listed provider serves fall back to the open ZDR pool |
| `SKILLOPT_MODEL` | `z-ai/glm-5.2` | authors eval sets and skill revisions, including reflection for the optional description pass |
| `STRONG_MODEL` | `SKILLOPT_MODEL` | serves novel requests (no skill matched) |
| `JUDGE_MODEL` | `google/gemini-2.5-flash` | the LLM judge; must differ from `SKILLOPT_MODEL` |
| `MIN_SCORE` | `0.53` | at/above: routable match; below: `related` band or novel. Calibrated to `EMBED_MODEL` (0.65 for bge-small) |
| `RELATED_SCORE` | `0.37` | floor of the `related` band; below it a task is novel (weak/strong escalation). Calibrated to `EMBED_MODEL` (0.45 for bge-small) |
| `EMBED_MODEL` | `onnx-community/Qwen3-Embedding-0.6B-ONNX` | router embedding model (q4 ONNX, ~15 ms/query on CPU; +7 top-1 over bge-small on a 297-query eval). Any fastembed name also works, but recalibrate the three score thresholds with it. Keep in sync with the Dockerfile's build arg |
| `EMBED_ONNX_FILE` | `onnx/model_q4.onnx` | which ONNX weight file to load inside the `EMBED_MODEL` repo; only relevant for ONNX exports that ship multiple quantizations |
| `BODY_TARGET_CHARS` | `6000` | length penalty starts past this body size |
| `LENGTH_PENALTY` | `0.10` | max score subtracted for a very long body |
| `LOOP_HEALTH_THRESHOLD` | `0.7` | the background loop proposes a change for skills whose mined mean score is below this |
| `LOOP_PASSES` | `body` | passes the loop runs per unhealthy skill, in order (e.g. `body,description,scripts`; `scripts` is skipped per skill without bundled scripts or exec checks) |
| `SKILLOPT_EPOCHS` | `2` | body pass: passes over the train set |
| `SKILLOPT_MINIBATCH` | `3` | body pass: train tasks reflected on per step |
| `SKILLOPT_MAX_EDITS` | `3` | body pass: ceiling on edits applied per step (the learning-rate cap) |
| `SKILLOPT_GATE_METRIC` | `mixed` | body pass: inner accept/reject metric, `hard`, `soft`, or `mixed` |
| `SKILLOPT_GATE_MIXED_WEIGHT` | `0.5` | weight on soft (mean-judge) when the metric is `mixed` |
| `MINE_MAX_JUDGE_CALLS` | `24` | maximum new trace-cluster judge calls per mining run; cached verdicts do not count, `<=0` is unlimited |
| `MINE_CLUSTER_THRESHOLD` | `0.90` | task cosine at or above this value shares a representative trace verdict |
| `SKILLOPT_ACCEPT_PENALTY` | `0.5` | how hard the inner loop docks a candidate whose train answers violate the skill's acceptance criteria (steers it to remove forbidden content, not append around it) |
| `PROMOTE_ACCEPT_BLOCK_RATE` | `0.5` | acceptance violations block promotion past this fraction of holdout answers; a smaller share is a ⚠ review warning. `0` = strict (any violation blocks), `>=1` = warning-only |
| `COMPAT_MODELS` | `AGENT_MODEL` | comma-separated serving models the cross-model compatibility sweep runs (`optimize-compat`) |
| `GEPA_ROLLOUTS` | `direct` | how the candidate search rolls out: `direct` (one call under the serving contract) or `agent` (full scaffold per rollout, ~10× cost). Legacy name, kept so existing `.env` files work |
| `RETENTION_WARN` | `0.5` | review warning when the challenger keeps less than this fraction of the champion body |
| `OPTIMIZE_COMPONENTS` | `body` | what may be rewritten; add `description` or `file:<path>` entries |
| `EXEC_SANDBOX` | `docker` | `docker` = locked-down container, `1` = bare subprocess (legacy), `off` = static checks only |
| `SANDBOX_IMAGE` | `ingot-optimize` | image sandbox containers run |
| `SANDBOX_RUNTIME` | (none) | optional container runtime, e.g. `runsc` for gVisor |
| `SKILL_USAGE_FILE` | `runs/skill_usage.json` | per-skill load counter: the MCP server increments it on every `get_skill` / `route_and_load` match, and the UI shows each skill's `uses` |
| `AUTH_MODE` | `password` (compose) | UI auth mode: `password` (HTTP Basic), `oidc` (Sign in with Google + roles, see [SSO](sso.md)), or `open` (no auth). When unset it is inferred as `password` if `AUTH_*` creds or an auth file exist, else `open`; set `AUTH_MODE=open` to force the UI open |
| `AUTH_USER` / `AUTH_PASSWORD` | `admin` / `ingot` (compose) | UI login for `password` mode. docker-compose sets these so the shared UI is gated by default, **change `AUTH_PASSWORD`** before exposing it |
| `AUTH_FILE` | `runs/auth.json` | additional `password`-mode users (salted PBKDF2) for more than one login; add with `python -m ui.auth add <name>` |
| `MAX_RUN_USD` | (none) | hard spend cap per optimize run: the ledger estimates cost from OpenRouter list prices after every call and aborts the run past the cap |
| `LANGFUSE_BASE_URL` | `http://langfuse-web:3000` | Langfuse endpoint every service traces to and mines from |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | `pk-lf-local-demo` / `sk-lf-local-demo` | project keys; defaults are the bundled stack's local demo literals |
| `LANGFUSE_PUBLIC_URL` | `http://localhost:3100` | where your browser reaches Langfuse (UI trace links) |

OIDC/SSO variables (`OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_REDIRECT_URL`,
`OIDC_ALLOWED_DOMAINS`, `OIDC_ROLE_MAP`, `OIDC_ROLE_CLAIM`, `SESSION_SECRET`) are covered in
[Sign in with Google (SSO)](sso.md).

Evidence-gate knobs (`PROMOTE_MIN_MARGIN`, `PROMOTE_MIN_SAMPLES`, `COLLISION_SCORE`,
`JUDGE_MODELS`) are covered in [The evidence gate](evidence-gate.md).

### Candidate generation

The body pass trains the skill body with **[SkillOpt](https://github.com/microsoft/SkillOpt)**'s
reflective loop (Yang et al., arXiv:2605.23904; MIT, © Microsoft), the skill document is treated
as trainable state and improved like a model is trained: epochs, minibatches, a learning rate, and
a validation gate, with no change to the serving model's weights. Per step
(`optimize/skillopt_loop.py`):

1. **Reflect** on the failing minibatch and propose bounded edits (append / insert_after / replace /
   delete), with a step buffer of prior failures and *rejected* edits fed back in so the optimizer
   stops re-proposing what the gate already threw out.
2. **Clip** the edit pool to a top-L budget the optimizer picks itself (the autonomous learning
   rate), keeping diffs minimal and reviewable.
3. **Gate**: apply the edits, roll the candidate out on the held-out selection tasks, and accept it
   only if it strictly improves the `hard` / `soft` / `mixed` metric (`SKILLOPT_GATE_METRIC`). An
   epoch-end slow/meta step consolidates the whole epoch's change.

All SkillOpt code is imported from the pinned `skillopt` package and funnelled through the single
seam `optimize/skillopt_bridge.py` (its prompts vendored under `optimize/skillopt_prompts/`); that
module and directory are the only things to touch when upgrading the dependency. The GEPA and
best-of-N body loops it replaced are **removed**, along with `OPTIMIZE_STRATEGY` /
`OPTIMIZE_CANDIDATES`. The optional description pass still uses GEPA as its search implementation,
but its authoring model is configured by `SKILLOPT_MODEL`. `GEPA_ROLLOUTS` retains its legacy name.

The champion's held-out A/B results are cached in `runs/eval-cache/`, keyed by (skill revision,
holdout tasks, serving model, judge), so repeat runs against an unchanged champion only pay for
the challenger's side.

### Cross-model compatibility

A skill body is tuned for one serving model, but skills often transfer. `optimize-compat` measures
that: it runs a skill's held-out tasks through several serving models (`COMPAT_MODELS`), each with
and without the skill body, and reports per-model **lift** (skill mean − no-skill mean) into
`runs/compat/<skill>.json`.

```bash
COMPAT_MODELS=qwen/qwen3-32b,openai/gpt-5.5,anthropic/claude-sonnet docker compose run --rm optimize-compat tailwind
```

Positive lift means the body helps that model; ~0 means the model already knows this and the body
is dead weight there (frontier models often need it least). The judge is held fixed so scores are
comparable across serving models; only the served model varies. Like the rest of the loop it uses
the local rollout + judge, so it needs no Langfuse.

### Writing eval task sets

Task sets are runtime artifacts, not shipped opinions; the repo commits none. They live in
`optimize/tasks/<skill>.yaml` (gitignored). Create one by hand or let `SKILLOPT_MODEL` auto-draft one
on the first CLI optimize run.

To author one manually:

1. Create `optimize/tasks/<skill>.yaml`, where `<skill>` exactly matches the directory name under
   `skills/`.
2. Add separate `train:` and `holdout:` lists. The candidate search sees only `train`; the evidence
   gate sees only `holdout`. A flat `tasks:` list is treated as train/holdout leakage and cannot
   produce a promotable result.
3. Give every item a self-contained user request under `task:` and explicit ground truth under
   `rubric:`. Use `deliverable:` when the expected result is not runnable code, and add `check:` for
   code whose behavior can be verified with a fixture and assertion.
4. Put at least three items in `holdout` (`PROMOTE_MIN_SAMPLES` defaults to 3). Make holdout requests
   exercise different wording or combinations of the same capabilities taught by train, not new
   facts absent from the training rubrics.
5. Run `docker compose run --rm optimize <skill>`. The command loads this exact file, searches on
   train, evaluates champion and challenger on holdout, and records the split in its evidence.

Anatomy:

```yaml
skill: accelerated-computing-cudf
train:                # the candidate search sees these; rubrics are the GROUND TRUTH it distills
- task: You trained a large XGBoost model, but GPU inference is bottlenecked by Python
    overhead and row-by-row execution. Which RAPIDS feature can run the trained forest
    efficiently without retraining it?
  rubric: "Must name cuML's Forest Inference Library (FIL), NOT Treelite. Must say FIL
    imports trained XGBoost, LightGBM, scikit-learn, and Treelite-format ensembles for
    batched GPU inference."
  deliverable: text   # optional: text | command | css | anything non-code disables the
                      # static "answer must contain a runnable Python block" check
holdout:              # the evidence gate ONLY trusts these; the candidate search never sees them
- task: Our fraud team has a LightGBM ensemble trained offline; scoring 200M rows nightly
    is too slow. Without retraining, how do we speed this up with RAPIDS?
  rubric: "Must recommend FIL loading the LightGBM model and discuss two trade-offs."
  deliverable: text
# optional per-task execution grounding (code tasks):
#   check:
#     fixture: open("input.txt", "w").write("hello")
#     assert: assert open("output.txt").read() == "HELLO"
routing:              # the description pass optimizes against these; the gate checks them
- task: "Which RAPIDS feature runs my trained forest on GPU without retraining?"
  expected: accelerated-computing-cudf
  harness: codex
- task: "Merge two PDF files and add page numbers."
  expected: null      # negatives: tasks that must NOT route here (no-route precision)
  harness: codex
```

The rules that make a set worth gating on: holdout must be a real split (a flat `tasks:` list is
flagged as leakage and can never promote); holdout tasks should recombine what train rubrics teach
rather than introduce new facts (your rubrics are how ground truth enters the system); and every
task an entire pool aces is dead weight.

#### Mined task candidates

`docker compose run --rm optimize-mine <skill>` does not modify the task YAML and there is currently
no UI action that promotes mined tasks. It prints and returns up to six `mined_tasks` for an operator
to review. Replace the reference-free placeholder rubric with explicit ground truth before relying
on a task for optimization evidence.

By default, mining paginates through every trace in the Langfuse project. `--limit N` is an explicit
newest-N operational cap, not the default. Before spending judge calls, it collapses formatting
duplicates and task paraphrases at `MINE_CLUSTER_THRESHOLD`. Every real use remains represented by
its cluster frequency, so repeated uses affect the weighted health score and failure counts without
being individually sent to an LLM.

Judge verdicts persist in `runs/mine-cache/judgments.json`, keyed by task, rubric, answer, judge
models, and judge prompt. An unchanged use is never re-judged. A cluster can reuse any cached member
verdict. A cold run judges at most `MINE_MAX_JUDGE_CALLS` new cluster representatives, prioritizing
high-frequency clusters. If a backlog remains, the background loop records `mining_backlog` and
defers its health and optimization decision. The next run continues from the cache. This prevents a
partially sampled traffic set from incorrectly declaring a skill healthy or triggering a promotion.

The mined-candidate selection is deterministic after representative judging:

1. Keep traces tagged with the skill, plus untagged or misrouted traces whose task ranks the skill in
   the embedding router's top five.
2. Cluster tasks by case, whitespace, and semantic similarity, then reuse or obtain one judge
   verdict per cluster. Cluster frequency weights aggregate health. Candidate difficulty is
   `1 - representative score`.
3. Embed the representative task text and exclude anything with cosine similarity at or above `0.90` to
   an existing train task.
4. Greedily choose the task with the largest `difficulty * novelty`, where novelty is one minus its
   highest cosine similarity to an already selected task. Stop at six tasks or when no task adds
   positive value. Each returned task includes its represented `occurrences`. This favors frequent,
   hard failures while preventing a set of near-paraphrases.

After inspecting mined failures, add accepted cases to `train`, not to the existing `holdout`.
Looking at a failure or its score before adding it to holdout contaminates promotion evidence. Add
new holdout cases only through a separate manual authoring pass, then keep that split stable while
comparing champion and challenger.

### Using your own Langfuse project

Langfuse is the default evals backend and comes up with `docker compose up` (UI at
**http://localhost:3100**, login `demo@local.dev` / `localdemo123`). Mining has no local fallback,
so it fails loudly unless a Langfuse-compatible endpoint is reachable. To point ingot at an
existing Langfuse project (Cloud or self-hosted elsewhere) instead of the bundled one, set all
three in `.env`:

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-...                  # your project's keys: Project Settings -> API Keys
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com   # or your self-hosted URL
LANGFUSE_PUBLIC_URL=https://cloud.langfuse.com # optional: where your browser reaches it
```

Start Ingot with the external-Langfuse override. Keep both files in subsequent Compose commands:

```bash
docker compose -f docker-compose.yml -f docker-compose.external-langfuse.yml up -d
docker compose -f docker-compose.yml -f docker-compose.external-langfuse.yml \
  run --rm optimize-mine <skill>
```

The override removes dependencies on `langfuse-web` and places the bundled Langfuse services and
datastores behind an inactive profile, so they are not started or waited on. It requires Docker
Compose 2.24.4 or newer for the standard `!override` merge tag.

One gotcha: `LANGFUSE_BASE_URL` must be reachable from inside the containers (not
`http://localhost:<port>`, which inside a container is the container itself; use
`http://host.docker.internal:<port>` or your host's LAN IP).

Securing the bundled Langfuse and connecting a non-Langfuse evals platform (Arize, …) are covered
in [Using your own evals platform](mcp-integration.md#using-your-own-evals-platform) and
[Security](security.md).


### Optional shared skill roots

The Docker demo reads and writes `skills/`. To route across additional libraries, set
`SKILL_ROUTER_PATHS` to a platform-separated list of directories:

```bash
export SKILL_ROUTER_PATHS="$HOME/Source/team-skills:$HOME/.agents/skills"
docker compose up --build
```

The local `skills/` root is searched first; the first duplicate name wins with a warning. Optional
`metadata.skill-router` frontmatter can restrict automatic matches by harness, project path,
platform, required tools/MCPs, trust, activation mode, priority, and conflicts.
