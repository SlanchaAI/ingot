"""Parallel best-of-N with racing — the default body-pass strategy (OPTIMIZE_STRATEGY=parallel).

Where GEPA serializes ~10-15 propose→evaluate→reflect iterations (tens of minutes wall-clock),
this runs three concurrent waves and stops:

1. baseline  — the seed skill is rolled out on every train task at once; its judge feedback
               becomes the failure evidence the authors write against, sharpened by one
               diagnosis call that attributes failures to skill sections (see diagnose())
2. author    — N candidate rewrites are drafted in parallel by the teacher model, each steered
               by a different angle so the pool isn't N copies of the same idea
3. race      — successive halving: every survivor answers the next train task (all rollouts
               concurrent), the bottom half is dropped, repeat until the tasks run out

The finalists' cumulative mean (minus the shared length penalty) picks the winner; a winner that
doesn't beat the seed returns the seed unchanged. The trade against GEPA is deliberate: no
failure-driven refinement between candidates, in exchange for wall-clock bounded by the slowest
single call per wave. The held-out A/B gate in optimize.ab is unchanged either way — this module
only replaces the inner loop. Set OPTIMIZE_STRATEGY=gepa (or --gepa) for the reflective loop.
"""
import hashlib
import json
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .gepa_loop import SkillAdapter, SERVE_TEMPLATE, assemble, length_penalty, make_reflection_lm

# Optional author-side web research (Tavily). Opt-in via TAVILY_API_KEY / TAVILY_KEY; without a
# key it is a silent no-op. Research runs ONCE per optimize run (a shared brief all authors
# receive — five authors independently searching would return five conflicting snapshots), only
# when the seed's failures look like knowledge gaps (correctness/completeness), and briefs are
# cached content-addressed so the autopilot re-optimizing the same skill costs zero extra
# searches. The judge NEVER gets research — rubrics stay the fixed measuring stick.
_RESEARCH_CACHE = Path(__file__).resolve().parent.parent / "runs" / "research-cache"
_RESEARCH_DIMS = ("correctness", "completeness")
_MAX_BRIEF_CHARS = 4000


def _tavily_key() -> str:
    return os.environ.get("TAVILY_API_KEY") or os.environ.get("TAVILY_KEY") or ""


def _tavily_search(query: str, key: str) -> str:
    """One search -> compact findings text ('' on any failure — research must never kill a run)."""
    body = json.dumps({"api_key": key, "query": query, "max_results": 3,
                       "include_answer": True}).encode()
    req = urllib.request.Request("https://api.tavily.com/search", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=20))
    except Exception:
        return ""
    parts = [r.get("answer") or ""]
    parts += [f"[{x.get('title', '')}] {x.get('content', '')[:400]}" for x in r.get("results", [])]
    return "\n".join(p for p in parts if p)


def research_brief(baseline: list, log=print) -> str:
    """A shared web-research brief for the author wave, or '' when research shouldn't run:
    no key configured, or the seed's failures aren't knowledge-shaped."""
    from .judge import failed_dimensions
    key = _tavily_key()
    if not key:
        return ""
    weak = sorted((r for r in baseline if r[1] < 1.0), key=lambda r: r[1])
    dims = {d for r in weak for d in failed_dimensions(r[2].get("dimensions", {}))}
    if not dims & set(_RESEARCH_DIMS):
        return ""
    queries = [r[2]["task"][:300] for r in weak[:3]]
    cache_key = hashlib.sha256(json.dumps(queries, sort_keys=True).encode()).hexdigest()[:16]
    cache_path = _RESEARCH_CACHE / f"{cache_key}.json"
    if cache_path.exists():
        log(f"[bestofn] research brief reused from cache ({cache_path.name})")
        return json.loads(cache_path.read_text())["brief"]
    log(f"[bestofn] researching {len(queries)} failing topics (Tavily)…")
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        findings = list(pool.map(lambda q: _tavily_search(q, key), queries))
    brief = "\n\n".join(f for f in findings if f)[:_MAX_BRIEF_CHARS]
    if brief:
        _RESEARCH_CACHE.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"queries": queries, "brief": brief}))
    return brief

_MAX_DIAG_CHARS = 3000

_DIAGNOSE_PROMPT = """You are diagnosing an agent skill before it is rewritten. Below are its
components, the train tasks it must satisfy, and the judge's feedback where the current version
fails.

Skill components:
{seed}

Train tasks and rubrics:
{tasks}

Observed failures (judge feedback):
{failures}

Tasks it already handles well:
{passes}

Attribute each failure to the specific section(s) of the skill responsible, classifying every
defect as one of: missing (needed guidance is absent), insufficient (present but incomplete or
stale), or incorrect (present but wrong). Then list the sections NO failure implicates — a
rewrite must preserve those. Output a compact plain-text report under exactly two headings,
"IMPLICATED" and "PRESERVE" — no preamble."""


def diagnose(seed: dict[str, str], task_text: str, failures: str, passes: str,
             reflection_lm, log=print) -> str:
    """Root-cause attribution before the author wave — the SkillForge paper's Skill Diagnostician
    (Liu et al., "SkillForge", arXiv:2604.08618, §2.6.2) reduced to one call: failures mapped to
    the specific skill sections responsible, each defect typed with the paper's missing /
    insufficient / incorrect taxonomy, plus the sections nothing implicates, which every author
    (the minimal-edit angle especially) is told to preserve. Returns '' on any failure —
    diagnosis only steers, it must never kill a run."""
    try:
        report = reflection_lm(_DIAGNOSE_PROMPT.format(
            seed=json.dumps(seed, indent=2), tasks=task_text, failures=failures, passes=passes))
    except Exception as e:
        log(f"[bestofn] diagnosis failed ({type(e).__name__}) — authors get raw judge feedback only")
        return ""
    return (report or "").strip()[:_MAX_DIAG_CHARS]


_MAX_WORKERS = 16   # per-wave rollout/author concurrency (hosted providers handle this fine)

# One steering angle per candidate slot (cycled past N) — cheap diversity so parallel blind
# drafts explore different regions instead of resampling one. Angles steer STYLE, never deletion:
# an angle that rewards cutting produced a challenger that halved a good body (caught by the gate,
# but prevention belongs here). The first angle is the SkillForge optimizer's Minimal
# Modification / Do No Harm discipline (Liu et al., "SkillForge", arXiv:2604.08618) run as one
# competing candidate rather than a rule: when the minimal edit wins the race the reviewer gets a
# clean additive diff, and when it loses, the fuller rewrite earned its changes on the same tasks.
_ANGLES = (
    "Make the smallest additive edit that fixes the observed failures: preserve the seed's "
    "structure and wording everywhere the failure evidence does not implicate, and only add or "
    "correct where it does.",
    "Tighten the wording ruthlessly — but preserve every operation the skill already covers.",
    "Include one worked example per major operation, with the exact expected output.",
    "Lead with edge cases and error handling: what goes wrong and the rule that prevents it.",
    "Structure as a decision checklist the model walks top-to-bottom for every task.",
    "Optimize for output-format compliance: exactly what the final answer must and must not contain.",
    "Group guidance by task family, most common first, one crisp rule each.",
)

_AUTHOR_PROMPT = """You are improving an agent skill. A skill component set is served to a smaller
model as its system prompt; the quality of its answers is judged per task against a rubric.

Current components (the seed):
{seed}

Frozen context served alongside (do NOT rewrite this):
{frozen}

Train tasks and rubrics it must satisfy:
{tasks}

Observed failures of the seed on these tasks (judge feedback):
{failures}
{diagnosis}{research}

Component roles are fixed: `description` is ONLY a routing trigger matched by embedding similarity
against the user's task — keep it a concise 'Use this skill when…' summary of trigger phrases,
never behavioral instructions. Every how-to-behave rule belongs in `body`.

Angle for THIS draft: {angle}

Tasks the seed ALREADY handles well — the guidance enabling these must survive your rewrite:
{passes}

Rewrite the component(s) {components} to fix the observed failures. Change ONLY what the failure
evidence implicates. Deletions need evidence: never remove guidance for operations these tasks
don't show failing — tighten or restructure it instead.
{format_instructions}"""

_RAW_FORMAT = ("Output ONLY the full new text of `{component}` — no preamble, no code fence, "
               "no commentary.")
_JSON_FORMAT = ('Output ONLY a JSON object mapping each component name to its full new text, '
                'e.g. {{"body": "..."}} — no preamble, no commentary.')


def _parse_candidate(text: str, seed: dict[str, str]) -> dict[str, str] | None:
    """The author's reply -> component dict, or None when unusable."""
    text = (text or "").strip()
    if not text:
        return None
    if len(seed) == 1:
        component = next(iter(seed))
        if text.startswith("```"):   # tolerate a fenced reply despite the instruction
            text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return {component: text}
    dec = json.JSONDecoder()
    for i in range(len(text)):
        if text[i] != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and set(obj) >= set(seed):
            return {k: str(obj[k]) for k in seed}
    return None


def _score(candidate: dict[str, str], raw_scores: list[float]) -> float:
    return (sum(raw_scores) / len(raw_scores) - length_penalty(candidate.get("body", ""))
            if raw_scores else 0.0)


def _score_remaining(field, survivors, remaining, rollout, scores, round_no, log) -> None:
    """Settled-pool fast path: the halving cut can drop nobody below 2 finalists, so sequencing
    the remaining rounds buys nothing. Score every remaining task for every survivor in one
    concurrent wave instead."""
    jobs = [(i, t) for i in survivors for t in remaining]
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(jobs))) as pool:
        results = list(pool.map(lambda j: rollout(field[j[0]], j[1]), jobs))
    for (i, _), r in zip(jobs, results):
        scores[i].append(r[1])
    log(f"[bestofn] race settled to {len(survivors)} finalist(s) after round {round_no}; "
        f"scored the remaining {len(remaining)} task(s) in one wave")


def run_bestofn(seed: dict[str, str], tasks: list[dict], frozen: dict[str, str] | None = None,
                candidates: int | None = None, log=print) -> tuple[dict[str, str], float, float]:
    """Drop-in for gepa_loop.run_gepa: returns (best_components, seed_score, best_score) where both
    scores are means over the full train set (the seed from wave 1, the winner from the race)."""
    candidates = candidates or int(os.environ.get("OPTIMIZE_CANDIDATES", "5"))
    if not tasks:
        log("[bestofn] no train tasks — nothing to race, keeping the seed.")
        return seed, 0.0, 0.0
    adapter = SkillAdapter(frozen)

    def rollout(components: dict[str, str], ex: dict):
        system = SERVE_TEMPLATE.format(body=assemble({**(frozen or {}), **components}))
        return adapter._rollout(system, ex)   # (answer, judge score, trajectory w/ feedback)

    # wave 1 — seed baseline on every train task at once; its failures brief the authors
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(tasks))) as pool:
        baseline = list(pool.map(lambda ex: rollout(seed, ex), tasks))
    seed_score = _score(seed, [r[1] for r in baseline])
    failures = "\n".join(f"- task: {r[2]['task']}\n  feedback: {r[2]['feedback']}"
                         for r in baseline if r[1] < 1.0) or "- (none — every task already passes)"
    passes = "\n".join(f"- {r[2]['task']} (judge score {r[1]:.2f})"
                       for r in baseline if r[1] >= 0.8) or "- (none yet)"
    brief = research_brief(baseline, log=log)
    research = (f"\nFresh web research on the failing topics (may postdate your training — "
                f"treat it as authoritative over your priors):\n{brief}\n") if brief else ""
    reflection_lm = make_reflection_lm()
    task_text = "\n".join(f"- task: {t['task']}\n  rubric: {t.get('rubric', '')}" for t in tasks)
    # SkillForge-style root cause attribution: skipped when the seed passes everything (nothing
    # to attribute).
    diag = (diagnose(seed, task_text, failures, passes, reflection_lm, log=log)
            if any(r[1] < 1.0 for r in baseline) else "")
    if diag:
        log("[bestofn] diagnosed failures against skill sections (root cause attribution)")
    diagnosis = (f"\nDiagnostic report — failures attributed to skill sections; fix what "
                 f"IMPLICATED names, keep what PRESERVE names:\n{diag}\n") if diag else ""
    log(f"[bestofn] seed scores {seed_score:.3f} on {len(tasks)} train tasks; "
        f"authoring {candidates} candidates in parallel…")

    # wave 2 — N parallel drafts, one angle each
    fmt = (_RAW_FORMAT.format(component=next(iter(seed))) if len(seed) == 1 else _JSON_FORMAT)

    def author(i: int) -> dict[str, str] | None:
        prompt = _AUTHOR_PROMPT.format(
            seed=json.dumps(seed, indent=2), frozen=json.dumps(frozen or {}, indent=2),
            tasks=task_text, failures=failures, passes=passes, diagnosis=diagnosis,
            research=research,
            angle=_ANGLES[i % len(_ANGLES)], components=sorted(seed), format_instructions=fmt)
        try:
            return _parse_candidate(reflection_lm(prompt), seed)
        except Exception as e:                      # one dead author must not kill the wave
            log(f"[bestofn] author {i} failed ({type(e).__name__}) — dropped")
            return None

    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, candidates)) as pool:
        pool_results = list(pool.map(author, range(candidates)))
    field = [c for c in pool_results if c]
    if not field:
        log("[bestofn] no parseable candidates — keeping the seed.")
        return seed, seed_score, seed_score

    # wave 3+ — successive halving over the train tasks; every round is one concurrent wave
    scores: dict[int, list[float]] = {i: [] for i in range(len(field))}
    survivors = list(range(len(field)))
    for round_no, ex in enumerate(tasks):
        if len(survivors) <= 2:
            _score_remaining(field, survivors, tasks[round_no:], rollout, scores, round_no, log)
            break
        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(survivors))) as pool:
            results = list(pool.map(lambda i: rollout(field[i], ex), survivors))
        for i, r in zip(survivors, results):
            scores[i].append(r[1])
        survivors.sort(key=lambda i: -_score(field[i], scores[i]))
        cut = max(2, (len(survivors) + 1) // 2)
        dropped = survivors[cut:]
        survivors = survivors[:cut]
        log(f"[bestofn] race round {round_no + 1}/{len(tasks)} ({ex['task'][:50]}…): "
            f"{len(survivors)} candidate(s) advance"
            + (f", {len(dropped)} dropped" if dropped else ""))

    finalists = [i for i in survivors if len(scores[i]) == len(tasks)]
    best_i = max(finalists or survivors, key=lambda i: _score(field[i], scores[i]))
    best_score = _score(field[best_i], scores[best_i])
    if best_score <= seed_score:
        log(f"[bestofn] best candidate {best_score:.3f} does not beat seed {seed_score:.3f} — "
            f"keeping the seed.")
        return seed, seed_score, best_score
    log(f"[bestofn] winner: candidate {best_i} at {best_score:.3f} (seed {seed_score:.3f})")
    return field[best_i], seed_score, best_score
