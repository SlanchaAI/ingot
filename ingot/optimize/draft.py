"""Auto-draft an eval task set for a skill that doesn't have one yet, so a freshly created skill is
immediately optimizable. The authoring model (SKILLOPT_MODEL) reads the skill's description + body and
writes train/holdout tasks with judge rubrics, split by *operation* so the holdout tests
generalization, not recall. Kept out of the MCP server (no LLM in its hot serving path); the
optimizer calls it on demand when `ingot/optimize/tasks/<skill>.yaml` is missing."""
import json
import os
import re

import yaml
from langchain_openai import ChatOpenAI

from . import client_kwargs, skillopt_model, teacher_base_url
from . import usage as usage_ledger

MODEL = skillopt_model()

_PROMPT = """You are writing an evaluation set for an AI "skill" (reusable task instructions).

SKILL NAME: {name}
SKILL DESCRIPTION: {description}
SKILL BODY (what the agent follows):
{body}

Write {n} concrete, self-contained eval tasks that exercise DISTINCT capabilities of this skill.
Each task is a realistic user request the skill should handle, plus a short grading rubric for an
LLM judge (what a correct answer must contain). Phrase tasks so the answer is the deliverable itself
(e.g. "Write Python code that…"), not a request to go find files.

Each task also carries a CHECKLIST of {items} independent checks that decide its score. Write checks
a grader can answer without re-reading the whole answer, and that a good and a bad answer would
genuinely split on:

- Each check tests ONE observable property. "Handles the empty input case" is a check; "is high
  quality" is not.
- Make them specific to THIS task, not generic writing advice. Prefer things the skill body says
  matter.
- id: short snake_case, unique within the task. weight: 1 (minor) to 5 (the point of the task).
- dimension: one of correctness, completeness, instruction_following, efficiency.

Return ONLY JSON with exactly {n} items, each covering a different operation/capability:
{{"tasks": [{{"task": "...", "rubric": "...",
             "checklist": [{{"id": "...", "criterion": "...", "weight": 3,
                             "dimension": "correctness"}}, ...]}}, ...]}}"""


def _llm():
    return ChatOpenAI(model=MODEL, temperature=0.4, **client_kwargs(teacher_base_url()))


_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


def _clean_checklist(raw) -> list[dict]:
    """Keep only checks a grader can actually apply, and drop the rest rather than shipping a
    rubric with unusable items in it. An empty result is fine: judge() falls back to its default
    checklist, which still grades four dimensions independently."""
    from .judge import DIMENSIONS
    out, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        item_id, criterion = str(item.get("id", "")).strip().lower(), str(item.get("criterion", "")).strip()
        if not _ID_RE.match(item_id) or item_id in seen or len(criterion) < 8:
            continue
        try:
            weight = min(5, max(1, int(item.get("weight", 1))))
        except (TypeError, ValueError):
            weight = 1
        dimension = str(item.get("dimension", "")).strip().lower()
        seen.add(item_id)
        out.append({"id": item_id, "criterion": criterion, "weight": weight,
                    "dimension": dimension if dimension in DIMENSIONS else "correctness"})
    return out


def draft_tasks(name: str, description: str, body: str, n: int = 8, items: int = 6) -> dict:
    """Draft n tasks, each with its own weighted checklist, split evenly into train/holdout
    (disjoint operations)."""
    msg = _llm().invoke(_PROMPT.format(name=name, description=description, body=body[:6000],
                                       n=n, items=items))
    usage_ledger.add("draft", getattr(msg, "usage_metadata", None))
    m = re.search(r"\{.*\}", msg.content, re.DOTALL)
    tasks = (json.loads(m.group(0)) if m else {}).get("tasks", [])
    tasks = [{"task": str(t["task"]), "rubric": str(t.get("rubric", "")),
              "checklist": _clean_checklist(t.get("checklist"))}
             for t in tasks if t.get("task")]
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
    checks = sum(len(t["checklist"]) for t in data["train"] + data["holdout"])
    bare = [t for t in data["train"] + data["holdout"] if not t["checklist"]]
    log(f"[draft] wrote {len(data['train'])} train + {len(data['holdout'])} holdout tasks, "
        f"{checks} graded checks → {path}")
    if bare:  # silently falling back to the default checklist would read as a richer set than it is
        log(f"[draft] {len(bare)} task(s) got no usable checklist and will grade on the default "
            f"four dimensions; edit {path} to add checks that matter for them.")
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
                         f"a routing: block in ingot/optimize/tasks/{name}.yaml.")
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
