"""SkillOpt integration — the single seam between skill_router and microsoft/SkillOpt (MIT).

Everything that imports from `skillopt` lives here, so upgrading is: bump `skillopt==` in
requirements, re-copy any changed prompt into `skillopt_prompts/` (see its README), and run the
bridge/loop tests. Nothing else in the repo imports the package.

What we take from the pinned package (pure code, upgrades on a version bump):
  * `select_gate_score` / `evaluate_gate` — the hard/soft/mixed held-out gate metric  (mechanic #4)
  * `apply_patch_with_report`             — apply append/insert_after/replace/delete edits to the
                                            skill document, honoring its protected slow-update region (mechanic #2)
  * `extract_json`                        — tolerant JSON parsing of optimizer-model replies

What we drive ourselves (so every model call rides our ZDR/cost-tracked reflection LM, not
skillopt's backends): the four prompt-driven steps, using SkillOpt's own prompt text loaded from
the vendored `skillopt_prompts/` directory:
  * reflection with a step buffer of prior failures + rejected edits  (mechanic #1)
  * edit-budget clipping (rank a pool, keep the top-L)                (mechanic #2)
  * autonomous learning-rate (how many edits to apply this step)      (mechanic #3)
  * epoch-end slow/meta consolidation                                 (mechanic #3)
"""
from __future__ import annotations

import os
from pathlib import Path

# Pure, dependency-light imports from the pinned skillopt package. None of these pull in
# skillopt.model (which eagerly imports optional backends), so a core-only install is enough.
from skillopt.evaluation.gate import evaluate_gate, select_gate_score  # noqa: F401  (re-exported)
from skillopt.optimizer.skill import apply_patch_with_report
from skillopt.utils.json_utils import extract_json

SKILLOPT_VERSION = "0.2.0"
_PROMPTS_DIR = Path(__file__).resolve().parent / "skillopt_prompts"
_VALID_OPS = {"append", "insert_after", "replace", "delete"}


def load_prompt(name: str) -> str:
    """Load a vendored SkillOpt prompt (the wheel ships no prompt files; see skillopt_prompts/README)."""
    return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _edits_from_reply(obj) -> list[dict]:
    """Pull a clean edit list out of an analyst reply. Accepts the analyst_error shape
    ({"patch": {"edits": [...]}}) or a bare {"edits": [...]}; drops malformed/incomplete edits so a
    single bad edit never aborts the step (apply_patch_with_report would skip them anyway)."""
    if not isinstance(obj, dict):
        return []
    container = obj.get("patch") if isinstance(obj.get("patch"), dict) else obj
    edits = []
    for e in container.get("edits") or []:
        if not isinstance(e, dict):
            continue
        op = str(e.get("op", "")).strip()
        if op not in _VALID_OPS:
            continue
        content = str(e.get("content", "")).strip()
        target = str(e.get("target", "")).strip()
        if op == "append" and not content:
            continue
        if op in ("insert_after", "replace") and not (target and content):
            continue
        if op == "delete" and not target:
            continue
        edits.append({"op": op, "content": content, "target": target})
    return edits


def _describe(edit: dict) -> str:
    op, target, content = edit.get("op", ""), edit.get("target", ""), edit.get("content", "")
    bits = [f"op={op}"]
    if target:
        bits.append(f'target="{target[:80]}"')
    if content:
        bits.append(f'content="{content[:120]}"')
    return " ".join(bits)


def reflect_edits(skill: str, trajectories: list[dict], buffer_context: str, budget: int,
                  reflection_lm) -> tuple[list[dict], list[dict]]:
    """SkillOpt's analyst_error step (mechanic #1 + #2): failed minibatch trajectories + the step
    buffer -> a pool of at-most-`budget` skill edits. Returns (edits, failure_summary)."""
    traj_lines = "\n".join(
        f"[{i}] task: {t.get('task', '')}\n    feedback: {t.get('feedback', '')}"
        for i, t in enumerate(trajectories, 1))
    user = (f"## Current Skill\n{skill}\n\n"
            f"## Failed trajectories in this minibatch ({len(trajectories)})\n{traj_lines}\n\n"
            f"## Step buffer — failure patterns and rejected edits from prior steps\n"
            f"{buffer_context or '(none yet)'}\n\n"
            f"Maximum number of edits (the budget L): {budget}")
    obj = extract_json(reflection_lm([{"role": "system", "content": load_prompt("analyst_error")},
                                      {"role": "user", "content": user}])) or {}
    return _edits_from_reply(obj), list(obj.get("failure_summary") or [])


def rank_edits(skill: str, edits: list[dict], budget: int, reflection_lm) -> list[dict]:
    """SkillOpt's ranking/clip step (mechanic #2): keep the top-`budget` most impactful edits.
    Falls back to the first `budget` edits if the ranking reply is unusable."""
    if len(edits) <= budget:
        return edits
    pool = "\n".join(f"[{i}] {_describe(e)}" for i, e in enumerate(edits))
    user = (f"## Current Skill\n{skill}\n\n## Proposed edit pool\n{pool}\n\n"
            f"Select the top {budget} edits.")
    obj = extract_json(reflection_lm([{"role": "system", "content": load_prompt("ranking")},
                                      {"role": "user", "content": user}])) or {}
    idx = [i for i in (obj.get("selected_indices") or []) if isinstance(i, int) and 0 <= i < len(edits)]
    chosen = [edits[i] for i in idx][:budget]
    return chosen or edits[:budget]


def decide_edit_budget(skill: str, edits: list[dict], hard: float, soft: float, n: int,
                       buffer_context: str, reflection_lm, ceiling: int) -> int:
    """SkillOpt's autonomous learning-rate (mechanic #3): the optimizer chooses how many of the
    proposed edits to apply this step. Clamped to [0, min(ceiling, len(edits))]; on any failure
    falls back to the ceiling so the step still makes progress."""
    upper = min(ceiling, len(edits))
    if upper <= 1:
        return upper
    pool = "\n".join(f"[{i}] {_describe(e)}" for i, e in enumerate(edits))
    user = (f"## Current Skill\n{skill}\n\n## Proposed update items\n{pool}\n\n"
            f"## Rollout evidence\nhard={hard:.3f} soft={soft:.3f} minibatch_n={n}\n\n"
            f"## Step buffer\n{buffer_context or '(none yet)'}")
    try:
        obj = extract_json(reflection_lm([{"role": "system", "content": load_prompt("lr_autonomous")},
                                          {"role": "user", "content": user}])) or {}
        lr = int(obj.get("learning_rate"))
    except (TypeError, ValueError):
        return upper
    return max(0, min(upper, lr))


def apply_edits(skill: str, edits: list[dict]) -> tuple[str, list[dict]]:
    """Apply a ranked edit set to the skill document (SkillOpt's pure Update stage)."""
    return apply_patch_with_report(skill, {"edits": edits})


def gate_metric() -> str:
    return os.environ.get("SKILLOPT_GATE_METRIC", "mixed")


def gate_mixed_weight() -> float:
    return float(os.environ.get("SKILLOPT_GATE_MIXED_WEIGHT", "0.5"))


def score(hard: float, soft: float) -> float:
    """Project (hard pass-rate, soft mean-judge) onto the configured comparison metric (mechanic #4)."""
    return select_gate_score(hard, soft, gate_metric(), gate_mixed_weight())


def slow_update(prev_skill: str, cur_skill: str, longitudinal: str, reflection_lm) -> str:
    """SkillOpt's epoch-end slow/meta consolidation (mechanic #3): a longitudinal view of the same
    tasks under the epoch's start and end skill -> consolidated guidance text. '' on any failure
    (the slow update only refines; it must never break the epoch)."""
    user = (f"## Previous epoch skill\n{prev_skill}\n\n## Current epoch skill\n{cur_skill}\n\n"
            f"## Longitudinal comparison (same tasks, both skills)\n{longitudinal}")
    try:
        obj = extract_json(reflection_lm([{"role": "system", "content": load_prompt("slow_update")},
                                          {"role": "user", "content": user}])) or {}
    except Exception:
        return ""
    return str(obj.get("guidance") or obj.get("updated_guidance") or "").strip()
