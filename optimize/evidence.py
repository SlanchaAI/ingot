"""Portable evidence emitted by the route-and-improve loop for local, CI, UI, and API use."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


SCHEMA = "skill-router/evidence/v1"
ROUTING_SCHEMA = "skill-router/evidence/routing/v1"
_REPO_ROOT = Path(__file__).resolve().parent.parent


def recorded_path(path: Path) -> str:
    """How an evidence location is written into a pending record: relative to the repo root.
    A bundle written inside a container is then still resolvable from the host checkout, and the
    review surface has a path it can contain to runs/evidence."""
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def first_divergence(champion: list[dict], challenger: list[dict]) -> dict | None:
    for index in range(max(len(champion), len(challenger))):
        left = champion[index] if index < len(champion) else None
        right = challenger[index] if index < len(challenger) else None
        if left != right:
            return {"index": index, "champion": left, "challenger": right}
    return None


def build_evidence(summary: dict, champion_revision: str, challenger_revision: str) -> dict:
    champion = summary["ab"]["champion"]
    challenger = summary["ab"]["challenger"]
    champ_behavior = summary.get("behavior", {}).get("champion", [])
    chall_behavior = summary.get("behavior", {}).get("challenger", [])
    cases = []
    for index, (champ_score, chall_score) in enumerate(zip(champion["scores"], challenger["scores"])):
        left = champ_behavior[index] if index < len(champ_behavior) else []
        right = chall_behavior[index] if index < len(chall_behavior) else []
        cases.append({
            "index": index,
            "champion_score": champ_score,
            "challenger_score": chall_score,
            "score_delta": round(chall_score - champ_score, 6),
            "first_divergence": first_divergence(left, right),
        })
    return {
        "schema_version": SCHEMA,
        "skill": summary["skill"],
        "created": summary["created"],
        "dataset": summary["dataset"],
        "harness": summary.get("harness", "unknown"),
        "model": summary.get("model", "unknown"),
        "split": summary.get("split", {"kind": "unknown", "leakage": True}),
        "routing": summary.get("routing"),
        "champion": {"revision": champion_revision, "mean": champion["mean"],
                     "scores": champion["scores"], "tokens": champion["tokens"]},
        "challenger": {"revision": challenger_revision, "mean": challenger["mean"],
                       "scores": challenger["scores"], "tokens": challenger["tokens"]},
        "changed_components": summary.get("changed_components", []),
        "cases": cases,
        "gate": summary["gate"],
    }


def render_markdown(evidence: dict) -> str:
    champion, challenger = evidence["champion"], evidence["challenger"]
    gate = evidence["gate"]
    state = "PASS" if gate.get("promotable") else "BLOCKED"
    blocked = "; ".join(gate.get("blocked", [])) or "none"
    warnings = "; ".join(gate.get("warnings", [])) or "none"
    output_before = champion["tokens"].get("mean_output", 0)
    output_after = challenger["tokens"].get("mean_output", 0)
    lines = [
        f"# Behavioral Skill CI: {evidence['skill']}",
        "",
        f"**Gate:** {state}",
        f"**Champion revision:** `{champion['revision']}`",
        f"**Challenger revision:** `{challenger['revision']}`",
        f"**Dataset:** `{evidence['dataset']}`",
        f"**Split:** `{evidence['split']['kind']}` (leakage: {str(evidence['split']['leakage']).lower()})",
        "",
        "## Outcome",
        "",
        f"- Mean score: {champion['mean']:.3f} → {challenger['mean']:.3f}",
        f"- Mean output tokens: {output_before:.0f} → {output_after:.0f}",
        f"- Changed components: {', '.join(evidence['changed_components']) or 'none'}",
        f"- Blocking reasons: {blocked}",
        f"- Warnings: {warnings}",
        "",
        "## Cases",
        "",
        "| Case | Champion | Challenger | Delta | First structural divergence |",
        "|---:|---:|---:|---:|---|",
    ]
    for case in evidence["cases"]:
        fork = case["first_divergence"]
        fork_text = f"event {fork['index']}" if fork else "none"
        lines.append(f"| {case['index']} | {case['champion_score']:.3f} | "
                     f"{case['challenger_score']:.3f} | {case['score_delta']:+.3f} | {fork_text} |")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class RoutingRun:
    """One routing pass as its evidence bundle needs it: which skill was optimized, against which
    routing suite, the revision on each side, and what the inner loop, the router, and the gate
    said about it."""

    skill: str
    created: int
    dataset: str
    metrics: dict
    champion_revision: str
    challenger_revision: str
    inner_loop: dict
    gate: dict


def build_routing_evidence(run: RoutingRun) -> dict:
    """The routing pass's portable bundle. It carries router metrics instead of judged cases: the
    description is scored by the real embedding router, so there are no rollouts to diff."""
    return {
        "schema_version": ROUTING_SCHEMA,
        "skill": run.skill,
        "created": run.created,
        "dataset": run.dataset,
        "changed_components": ["description"],
        "inner_loop": run.inner_loop,
        "champion": {"revision": run.champion_revision, "routing": run.metrics["champion"]},
        "challenger": {"revision": run.challenger_revision, "routing": run.metrics["challenger"]},
        "parity": run.metrics.get("parity"),
        "gate": run.gate,
    }


def render_routing_markdown(evidence: dict) -> str:
    champion, challenger = evidence["champion"], evidence["challenger"]
    gate = evidence["gate"]
    parity = evidence.get("parity") or {}
    inner = evidence.get("inner_loop") or {}
    parity_text = f"{parity['rate']:.3f}" if parity.get("total") else "not exercised"
    lines = [
        f"# Routing evidence: {evidence['skill']}",
        "",
        f"**Gate:** {'PASS' if gate.get('promotable') else 'BLOCKED'}",
        f"**Champion revision:** `{champion['revision']}`",
        f"**Challenger revision:** `{challenger['revision']}`",
        f"**Routing suite:** `{evidence['dataset']}`",
        "",
        "## Outcome",
        "",
        f"- Blocking reasons: {'; '.join(gate.get('blocked', [])) or 'none'}",
        f"- Warnings: {'; '.join(gate.get('warnings', [])) or 'none'}",
        f"- Cross-harness parity: {parity_text}",
        "",
        "## Router metrics",
        "",
        "| Metric | Champion | Challenger | Delta |",
        "|---|---:|---:|---:|",
    ]
    for metric in ("top1", "recall_at_3", "no_route_precision"):
        before, after = champion["routing"][metric], challenger["routing"][metric]
        lines.append(f"| {metric} | {before:.3f} | {after:.3f} | {after - before:+.3f} |")
    if inner:
        lines += ["", f"Inner-loop score: {inner.get('seed_score', 0):.3f} -> "
                      f"{inner.get('best_score', 0):.3f} (budget {inner.get('budget', 'n/a')})"]
    return "\n".join(lines) + "\n"


def write_evidence(evidence: dict, root: Path) -> tuple[Path, Path]:
    """Write a bundle as `evidence.json` plus a rendered `EVIDENCE.md`, chosen by schema."""
    root.mkdir(parents=True, exist_ok=True)
    render = (render_routing_markdown if evidence.get("schema_version") == ROUTING_SCHEMA
              else render_markdown)
    json_path, md_path = root / "evidence.json", root / "EVIDENCE.md"
    for path, content in ((json_path, json.dumps(evidence, indent=2) + "\n"),
                          (md_path, render(evidence))):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content)
        temporary.replace(path)
    return json_path, md_path
