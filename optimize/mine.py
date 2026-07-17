"""Success/failure mining — the SkillForge paper's Failure Analyzer over *our* real traces.

Pulls logged agent runs from Langfuse, keeps the ones attributable to the skill being mined
(tagged with it, or ranking it in the embedding top-k for the task text — which also catches
traffic the skill *should* have served but didn't route), re-judges each outcome with the
multi-dimensional judge, and aggregates which failure dimensions (correctness / completeness /
instruction-following / efficiency) dominate — turning accumulated operational evidence into a
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

from .judge import DIMENSIONS, failed_dimensions, judge

LF_URL = os.environ.get("LANGFUSE_BASE_URL", "http://langfuse-web:3000")
LF_PK = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local-demo")
LF_SK = os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local-demo")


def fetch_traces(limit: int) -> list[dict]:
    """Recent traces with a recognizable task input and a non-empty answer output."""
    auth = base64.b64encode(f"{LF_PK}:{LF_SK}".encode()).decode()
    req = urllib.request.Request(f"{LF_URL}/api/public/traces?limit={limit}",
                                 headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())["data"]
    out = []
    for t in data:
        parsed = _task_answer(t.get("input"), t.get("output"))
        if parsed:
            task, rubric, answer = parsed
            out.append({"task": task, "rubric": rubric, "answer": answer, "tags": t.get("tags", [])})
    return out


def _task_answer(inp, ans):
    """(task, rubric, answer) from either trace shape: eval runs log a {task, rubric} input and a
    plain-string answer; live agent runs traced by the LangChain callback log LangGraph state —
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
    """Traces attributable to `skill`: tagged with it (the canary and external harnesses tag the
    routed skill), or ranking it in the embedding top-k for the task text. The rank check is what
    catches traffic the skill *should* have served but didn't route — under-triggering, the common
    routing failure — which a tag filter alone would attribute to the wrong skill."""
    from mcp_server.registry import load_skills
    from mcp_server.router import Router
    router = Router(load_skills())
    return [t for t in traces
            if skill in t.get("tags", [])
            or any(s["name"] == skill for s in router.suggest(t["task"], k=k, min_score=0.0))]


def mine(skill: str, limit: int = 50, log=print) -> dict:
    log(f"[mine] pulling recent traces from Langfuse for '{skill}'…")
    traces = fetch_traces(limit)
    if not traces:
        raise SystemExit("No usable traces found — run the agent / optimizer first to generate some.")
    total = len(traces)
    traces = relevant_traces(traces, skill)
    if not traces:
        raise SystemExit(f"No traces relevant to '{skill}' among the last {total} — run the agent "
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

    # the weakest real tasks become mined eval candidates for a targeted optimize run
    worst = sorted(zip(scores, traces), key=lambda x: x[0])[:6]
    mined = [{"task": t["task"], "rubric": t["rubric"] or "(reference-free)"} for _, t in worst]
    log(f"\n[mine] {len(mined)} weakest tasks mined as eval candidates → optimize on these next.")
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
