"""Portable evidence emitted by the route-and-improve loop for local, CI, UI, and API use."""
from __future__ import annotations

import json
from pathlib import Path


SCHEMA = "skill-router/evidence/v1"


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


def write_evidence(evidence: dict, root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    json_path, md_path = root / "evidence.json", root / "EVIDENCE.md"
    for path, content in ((json_path, json.dumps(evidence, indent=2) + "\n"),
                          (md_path, render_markdown(evidence))):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content)
        temporary.replace(path)
    return json_path, md_path
