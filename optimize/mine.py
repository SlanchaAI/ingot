"""Success/failure mining, the SkillForge paper's Failure Analyzer over *our* real traces.

Pulls logged agent runs from Langfuse, keeps the ones attributable to the skill being mined
(tagged with it, or ranking it in the embedding top-k for the task text, which also catches
traffic the skill *should* have served but didn't route), re-judges each outcome with the
multi-dimensional judge, and aggregates which failure dimensions (correctness / completeness /
instruction-following / efficiency) dominate, turning accumulated operational evidence into a
diagnosis of where a skill is weak, plus the weakest real tasks as mined eval candidates. This is
the signal that drives targeted optimization (the paper: Liu et al., "SkillForge: Forging
Domain-Specific, Self-Evolving Agent Skills", arXiv:2604.08618).

Usage: python -m optimize.mine <skill> [--limit 50]
"""
import argparse
import base64
import json
import os
import urllib.request
from collections import Counter, defaultdict
from typing import Callable, NamedTuple

from .judge import DIMENSIONS, failed_dimensions, judge

LF_URL = os.environ.get("LANGFUSE_BASE_URL", "http://langfuse-web:3000")
LF_PK = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local-demo")
LF_SK = os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local-demo")


def fetch_traces(limit: int) -> list[dict]:
    """Recent traces with a recognizable task input and a non-empty answer output, read from the
    configured evals backend's Langfuse-compatible trace API. Fails loudly when the backend is
    unreachable: mining has no other source of real traffic, so a silent empty result would read
    as 'nothing failing' when it means 'no traces'."""
    auth = base64.b64encode(f"{LF_PK}:{LF_SK}".encode()).decode()
    req = urllib.request.Request(f"{LF_URL}/api/public/traces?limit={limit}",
                                 headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())["data"]
    except OSError as e:
        raise SystemExit(
            f"evals backend unreachable at {LF_URL} ({e}). Mining reads traces from Langfuse (the "
            f"default) or a Langfuse-compatible endpoint; bring up the stack (`docker compose up`) "
            f"or point LANGFUSE_BASE_URL / _PUBLIC_KEY / _SECRET_KEY at your own provider. See "
            f"docs/mcp-integration.md (Using your own evals platform).")
    out = []
    for t in data:
        parsed = _task_answer(t.get("input"), t.get("output"))
        if parsed:
            task, rubric, answer = parsed
            out.append({"task": task, "rubric": rubric, "answer": answer, "tags": t.get("tags", [])})
    return out


def _task_answer(inp, ans):
    """(task, rubric, answer) from either trace shape: eval runs log a {task, rubric} input and a
    plain-string answer; live agent runs traced by the LangChain callback log LangGraph state ,
    {'messages': [...]} on both sides. Returns None for anything else (or an empty answer)."""
    if isinstance(inp, dict) and inp.get("task") and isinstance(ans, str) and ans.strip():
        return inp["task"], inp.get("rubric", ""), ans
    try:
        task = inp["messages"][0]["content"]
        answer = ans["messages"][-1]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(answer, list):  # some models return content blocks, not a plain string
        answer = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in answer)
    if isinstance(task, str) and task.strip() and isinstance(answer, str) and answer.strip():
        return task, "", answer
    return None


def relevant_traces(traces: list[dict], skill: str, k: int = 5) -> list[dict]:
    """Traces attributable to `skill`: tagged with it (external harnesses tag the
    routed skill), or ranking it in the embedding top-k for the task text. The rank check is what
    catches traffic the skill *should* have served but didn't route, under-triggering, the common
    routing failure, which a tag filter alone would attribute to the wrong skill."""
    from mcp_server.registry import load_skills
    from mcp_server.router import Router
    router = Router(load_skills())
    return [t for t in traces
            if skill in t.get("tags", [])
            or any(s["name"] == skill for s in router.suggest(t["task"], k=k, min_score=0.0))]


def _greedy_pick(difficulty, vecs, k: int) -> list[int]:
    """Difficulty-weighted greedy max-min: each pick maximizes difficulty x novelty, where
    novelty is 1 minus the max cosine to anything already picked. Hardest tasks win, but a
    near-paraphrase of a previous pick gains ~nothing, so picks spread across failure modes
    (specialize's lesson: judge failure dominates, embedding distance is only the coverage
    term). Stops when no candidate adds value: aced tasks (difficulty 0) are dead weight."""
    import numpy as np
    difficulty = np.asarray(difficulty, dtype=np.float32).copy()
    picked: list[int] = []
    novelty = np.ones(len(difficulty), dtype=np.float32)
    for _ in range(min(k, len(difficulty))):
        gains = difficulty * novelty
        i = int(np.argmax(gains))
        if gains[i] <= 0:
            break
        picked.append(i)
        novelty = np.minimum(novelty, 1.0 - vecs @ vecs[i])
        difficulty[i] = -1.0
    return picked


MINED_CANDIDATES = 6        # eval candidates surfaced per mine run
TRAIN_DUP_THRESHOLD = 0.90  # cosine to an existing train task at/above which a candidate leaks


def _train_dupes(vecs, skill: str, norm_embed, log):
    """Boolean mask of candidates that near-duplicate the skill's existing train tasks (the
    holdout must recombine train, not repeat it), or None when there is no train set."""
    import yaml
    from pathlib import Path
    tasks_file = Path(__file__).resolve().parent / "tasks" / f"{skill}.yaml"
    if not tasks_file.exists():
        return None
    data = yaml.safe_load(tasks_file.read_text()) or {}
    train_texts = [t["task"] for t in (data.get("train") or data.get("tasks") or [])]
    if not train_texts:
        return None
    dupes = (vecs @ norm_embed(train_texts).T).max(axis=1) >= TRAIN_DUP_THRESHOLD
    if dupes.any():
        log(f"[mine] {int(dupes.sum())} candidate(s) dropped as near-duplicates of "
            f"existing train tasks (leakage guard)")
    return dupes


def _normalized_embedder():
    """Return an embedding function that produces row-normalized float vectors. Task-to-task
    similarity (diversity + train-dup checks), so both sides use the document embedding."""
    import numpy as np
    from mcp_server.embedding import build_embedding

    embedder = build_embedding()

    def norm_embed(texts):
        matrix = np.array(list(embedder.embed(texts)), dtype=np.float32)
        return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)

    return norm_embed


def _candidate_representatives(traces: list[dict], scores: list[float], log) -> list[int]:
    """Keep the lowest-scoring representative of each formatting-equivalent task."""
    representatives = {}
    for index, trace in enumerate(traces):
        key = " ".join(trace["task"].casefold().split())
        previous = representatives.get(key)
        if previous is None or scores[index] < scores[previous]:
            representatives[key] = index
    indices = list(representatives.values())
    collapsed = len(traces) - len(indices)
    if collapsed:
        log(f"[mine] {collapsed} duplicate candidate(s) collapsed")
    return indices


class _RankingContext(NamedTuple):
    skill: str
    norm_embed: Callable
    log: Callable


def _rank_candidates(traces, scores, indices, context: _RankingContext) -> list[int]:
    """Rank representative tasks by failure difficulty and embedding coverage."""
    import numpy as np

    vecs = context.norm_embed([traces[index]["task"] for index in indices])
    difficulty = 1.0 - np.asarray([scores[index] for index in indices], dtype=np.float32)
    dupes = _train_dupes(vecs, context.skill, context.norm_embed, context.log)
    if dupes is not None:
        difficulty[dupes] = -1.0
    return [indices[index] for index in _greedy_pick(difficulty, vecs, MINED_CANDIDATES)]


def _select_candidates(traces: list[dict], scores: list[float], skill: str, log=print) -> list[int]:
    """Pick eval candidates from mined traces: difficulty from the judge (1 - score), spread
    by embedding coverage, and never a near-duplicate of the skill's existing train set."""
    # Collapse exact and formatting-only duplicates before semantic coverage selection. Keep the
    # lowest-scoring occurrence because it provides the strongest failure evidence.
    indices = _candidate_representatives(traces, scores, log)
    context = _RankingContext(skill, _normalized_embedder(), log)
    return _rank_candidates(traces, scores, indices, context)


def mine(skill: str, limit: int = 50, log=print) -> dict:
    log(f"[mine] pulling recent traces from Langfuse for '{skill}'…")
    traces = fetch_traces(limit)
    if not traces:
        raise SystemExit("No usable traces found; run the agent or a candidate pass first to "
                         "generate some.")
    total = len(traces)
    traces = relevant_traces(traces, skill)
    if not traces:
        raise SystemExit(f"No traces relevant to '{skill}' among the last {total}, run the agent "
                         "on matching traffic first (or check the skill name).")
    log(f"[mine] {len(traces)}/{total} recent traces relevant to '{skill}' "
        f"(tagged with it, or ranking it in the embedding top-5)")

    dim_failures = Counter()          # dimension -> how many traces failed it
    examples = defaultdict(list)      # dimension -> a few failing tasks
    scores = []
    for t in traces:
        j = judge(t["task"], t["rubric"], t["answer"])
        scores.append(j["score"])
        for d in failed_dimensions(j["dimensions"]):
            dim_failures[d] += 1
            if len(examples[d]) < 3:
                examples[d].append((t["task"][:70], j["dimensions"][d]))

    n = len(traces)
    mean = sum(scores) / n
    log(f"\n[mine] analyzed {n} real traces · mean judge score {mean:.2f} · "
        f"{sum(1 for s in scores if s < 0.5)} bad cases (score < 0.5)")
    log("[mine] failure dimensions (paper's Failure Analyzer), most common first:")
    for d in sorted(DIMENSIONS, key=lambda d: -dim_failures[d]):
        bar = "█" * round(10 * dim_failures[d] / n)
        log(f"    {d:<22} {dim_failures[d]:>3}/{n}  {bar}")
        for task, note in examples[d][:2]:
            log(f"        · {task!r} → {note}")

    # the weakest real tasks become mined eval candidates for a targeted optimize run,
    # difficulty-weighted and coverage-spread so they aren't six paraphrases of one failure
    picked = _select_candidates(traces, scores, skill, log=log)
    mined = [{"task": traces[i]["task"], "rubric": traces[i]["rubric"] or "(reference-free)"}
             for i in picked]
    log(f"\n[mine] {len(mined)} weakest tasks mined as eval candidates (coverage-spread) "
        f"→ optimize on these next.")
    return {"skill": skill, "traces": n, "mean_score": mean,
            "failure_dimensions": dict(dim_failures), "mined_tasks": mined}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("skill")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()
    from . import require_openrouter_key
    require_openrouter_key()
    mine(args.skill, limit=args.limit)
