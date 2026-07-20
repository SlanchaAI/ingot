"""Deterministic per-skill acceptance criteria — hard invariants every holdout answer must
satisfy, checked with no LLM in the loop. They ground the subjective judge at the promotion
gate: a challenger that wins the mean judge score but violates an invariant (e.g. a Tailwind v4
skill whose answer still emits the deprecated v3 `@tailwind` directives) is blocked regardless
of its score, exactly like the catastrophic-regression and routing-shadow gates alongside it.

Borrowed from Tool Forge's contract-ledger acceptance criteria, adapted from generated-tool
invariants (reject empty input, JSON on every path) to skill-answer invariants. Only `forbid`
(a regex that must NOT appear in any holdout answer) is supported: it is the one invariant
shape that is unambiguously universal across a skill's whole holdout set — a `require` that must
hold on every answer rarely does, since holdout tasks exercise different facts.

Task-file shape (optional; sibling to train/holdout/routing):

    acceptance:
    - id: no_v3_tailwind_directives
      forbid: '@tailwind\\s+(base|components|utilities)'
      description: v4 starts CSS with a single @import "tailwindcss"; the three @tailwind
        directives are v3.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


def load_criteria(skill: str, tasks_dir: Path) -> list[dict]:
    """Parse the optional `acceptance:` list from a skill's task YAML. A malformed regex raises
    here (author error) rather than being silently skipped — a safety invariant that quietly
    stops firing is worse than a loud failure."""
    path = tasks_dir / f"{skill}.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    criteria = []
    for entry in data.get("acceptance") or []:
        pattern = str(entry.get("forbid", "")).strip()
        if not pattern:
            continue
        criteria.append({
            "id": str(entry.get("id") or pattern),
            "forbid": re.compile(pattern, re.IGNORECASE),
            "description": str(entry.get("description", "")),
        })
    return criteria


def _violations(criteria: list[dict], answers: list[str]) -> list[tuple[float, str]]:
    """[(violation_rate, reason)] for each criterion some answer violates (empty = clean)."""
    out = []
    total = len(answers) or 1
    for c in criteria:
        hits = sum(1 for a in answers if a and c["forbid"].search(a))
        if hits:
            detail = f" ({c['description']})" if c["description"] else ""
            out.append((hits / total,
                        f"acceptance '{c['id']}': {hits}/{len(answers)} holdout answer(s) matched "
                        f"forbidden pattern{detail}"))
    return out


def evaluate(criteria: list[dict], answers: list[str]) -> list[str]:
    """Every violation reason (empty = clean) — the training-signal view the optimizer reads."""
    return [reason for _rate, reason in _violations(criteria, answers)]


def classify(criteria: list[dict], answers: list[str], block_rate: float = 0.5) -> tuple[list[str], list[str]]:
    """Split violations into (blocking, warning) by per-criterion violation rate: a forbidden
    pattern in MORE than `block_rate` of the answers means the challenger clearly didn't satisfy
    the criterion (block); a minority is a warning for the human to weigh with the evidence.
    block_rate=0 restores strict zero-tolerance (any violation blocks); block_rate>=1 makes
    acceptance a pure warning (never blocks)."""
    blocking, warning = [], []
    for rate, reason in _violations(criteria, answers):
        (blocking if rate > block_rate else warning).append(reason)
    return blocking, warning
