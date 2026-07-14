"""Success/failure mining — the SkillForge paper's Failure Analyzer over *our* real traces.

Pulls logged agent runs from Langfuse, re-judges each outcome with the multi-dimensional judge, and
aggregates which failure dimensions (correctness / completeness / instruction-following / efficiency)
dominate — turning accumulated operational evidence into a diagnosis of where a skill is weak, plus
the weakest real tasks as mined eval candidates. This is the signal that drives targeted optimization
(the paper: Liu et al., "SkillForge: Forging Domain-Specific, Self-Evolving Agent Skills",
arXiv:2604.08618).

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
    """Recent traces with a {task,...} input and a non-empty answer output."""
    auth = base64.b64encode(f"{LF_PK}:{LF_SK}".encode()).decode()
    req = urllib.request.Request(f"{LF_URL}/api/public/traces?limit={limit}",
                                 headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())["data"]
    out = []
    for t in data:
        inp, ans = t.get("input"), t.get("output")
        if isinstance(inp, dict) and inp.get("task") and isinstance(ans, str) and ans.strip():
            out.append({"task": inp["task"], "rubric": inp.get("rubric", ""), "answer": ans,
                        "tags": t.get("tags", [])})
    return out


def mine(skill: str, limit: int = 50, log=print) -> dict:
    log(f"[mine] pulling recent traces from Langfuse for '{skill}'…")
    traces = fetch_traces(limit)
    if not traces:
        raise SystemExit("No usable traces found — run the agent / optimizer first to generate some.")

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
    mine(args.skill, limit=args.limit)
