"""Auto-draft an eval task set for a skill that doesn't have one yet, so a freshly created skill is
immediately optimizable. The teacher model (GEPA_MODEL) reads the skill's description + body and
writes train/holdout tasks with judge rubrics, split by *operation* so the holdout tests
generalization, not recall. Kept out of the MCP server (no LLM in its hot serving path); the
optimizer calls it on demand when `optimize/tasks/<skill>.yaml` is missing."""
import json
import os
import re

import yaml
from langchain_openai import ChatOpenAI

from . import client_kwargs, teacher_base_url
from . import usage as usage_ledger

MODEL = os.environ.get("GEPA_MODEL", "z-ai/glm-5.2")  # the teacher authors evals, like it authors skills

_PROMPT = """You are writing an evaluation set for an AI "skill" (reusable task instructions).

SKILL NAME: {name}
SKILL DESCRIPTION: {description}
SKILL BODY (what the agent follows):
{body}

Write {n} concrete, self-contained eval tasks that exercise DISTINCT capabilities of this skill.
Each task is a realistic user request the skill should handle, plus a short grading rubric for an
LLM judge (what a correct answer must contain). Phrase tasks so the answer is the deliverable itself
(e.g. "Write Python code that…"), not a request to go find files.

Return ONLY JSON: {{"tasks": [{{"task": "...", "rubric": "..."}}, ...]}} with exactly {n} items,
each covering a different operation/capability."""


def _llm():
    return ChatOpenAI(model=MODEL, temperature=0.4, **client_kwargs(teacher_base_url()))


def draft_tasks(name: str, description: str, body: str, n: int = 8) -> dict:
    """Draft n tasks and split them evenly into train/holdout (disjoint operations)."""
    msg = _llm().invoke(_PROMPT.format(name=name, description=description, body=body[:6000], n=n))
    usage_ledger.add("draft", getattr(msg, "usage_metadata", None))
    m = re.search(r"\{.*\}", msg.content, re.DOTALL)
    tasks = (json.loads(m.group(0)) if m else {}).get("tasks", [])
    tasks = [{"task": str(t["task"]), "rubric": str(t.get("rubric", ""))} for t in tasks if t.get("task")]
    if len(tasks) < 4:
        raise SystemExit(f"draft_tasks: teacher returned only {len(tasks)} usable tasks for '{name}'.")
    half = len(tasks) // 2
    return {"skill": name, "train": tasks[:half], "holdout": tasks[half:]}


def draft_and_save(name: str, description: str, body: str, tasks_dir, n: int = 8, log=print):
    """Draft a task set and write it to tasks_dir/<name>.yaml. Returns the path."""
    from pathlib import Path
    log(f"[draft] no eval set for '{name}', teacher ({MODEL}) drafting {n} train/holdout tasks…")
    data = draft_tasks(name, description, body, n=n)
    path = Path(tasks_dir) / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100000))
    log(f"[draft] wrote {len(data['train'])} train + {len(data['holdout'])} holdout tasks → {path}")
    return path


_ROUTING_PROMPT = """You are writing ROUTING test cases for an embedding-based skill router.

SKILL NAME: {name}
SKILL DESCRIPTION (the routing trigger): {description}
SKILL BODY (for context):
{body}

Write {positives} realistic user requests that SHOULD route to this skill, vary the phrasing the
way real users type: some explicit, some indirect, different vocabulary. Then write {negatives}
requests that must route to NO skill at all: nearby-domain distractors and pure conversation
(thanks/greetings).

Return ONLY JSON: {{"positive": ["...", ...], "negative": ["...", ...]}}"""


def draft_routing_cases(name: str, description: str, body: str,
                        positives: int = 4, negatives: int = 2) -> list[dict]:
    """Teacher-drafted routing cases in the task-YAML `routing:` shape (expected / null negatives,
    mixed harnesses, parity flags on the first of each kind)."""
    msg = _llm().invoke(_ROUTING_PROMPT.format(name=name, description=description,
                                               body=body[:4000], positives=positives,
                                               negatives=negatives))
    usage_ledger.add("draft", getattr(msg, "usage_metadata", None))
    m = re.search(r"\{.*\}", msg.content, re.DOTALL)
    data = json.loads(m.group(0)) if m else {}
    pos = [str(t) for t in data.get("positive", []) if str(t).strip()]
    neg = [str(t) for t in data.get("negative", []) if str(t).strip()]
    if len(pos) < 2 or len(neg) < 1:
        raise SystemExit(f"draft_routing_cases: teacher returned {len(pos)} positive / {len(neg)} "
                         f"negative cases for '{name}', need at least 2/1. Re-run or hand-write "
                         f"a routing: block in optimize/tasks/{name}.yaml.")
    cases = []
    for i, task in enumerate(pos):
        case = {"task": task, "expected": name, "harness": "claude" if i == 1 else "codex"}
        if i == 0:
            case["parity"] = True
        cases.append(case)
    for i, task in enumerate(neg):
        case = {"task": task, "expected": None, "harness": "codex"}
        if i == 0:
            case["parity"] = True
        cases.append(case)
    return cases


def draft_and_append_routing(name: str, description: str, body: str, tasks_dir, log=print) -> list[dict]:
    """Draft routing cases and persist them into tasks_dir/<name>.yaml's routing: block."""
    from pathlib import Path
    log(f"[draft] no routing cases for '{name}', teacher ({MODEL}) drafting some…")
    cases = draft_routing_cases(name, description, body)
    path = Path(tasks_dir) / f"{name}.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {"skill": name}
    data["routing"] = cases
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100000))
    log(f"[draft] wrote {len(cases)} routing cases → {path}")
    return cases
