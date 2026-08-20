"""Read a harness x model matrix off disk for display.

`harbor_eval` writes `runs/harbor/<skill>.json`; `harbor_rescore` writes `<skill>.rescored.json`
from the same job directories when the scoring rules change. The two carry the same rows under
different keys (`harnesses` and `combinations`), and the rescored file is the corrected one wherever
it exists — the first grid shipped two fabricated cells that only rescoring removed.

Stdlib only, and no judging: this reads results, it does not produce them.

The reporting rules here are the ones the matrix violated the first time it was run:

- a combination that failed carries no `lift` at all, so nothing downstream can render it as a zero
  in a column of measurements;
- every row carries the `n` it was measured over, because a lift over two tasks and one over four
  are different claims;
- and if the control arms sit near the top of the scale, the instrument could not discriminate and
  the ranking means nothing, so the ceiling is reported instead of the winner.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from ingot import paths

from .harbor_targets import TARGETS

HARBOR_DIR = paths.runs() / "harbor"

# Above this, the controls are close enough to the top of the scale that the remaining headroom is
# smaller than the judge's own noise (~0.10 observed across re-runs of an unchanged arm), so a
# treatment cannot separate from its control whatever the skill does. Reporting a "best combination"
# off a matrix in this state is reporting the noise.
CEILING = 0.75


def matrix_path(skill: str, root: Path | None = None) -> Path | None:
    """The newest of the rescored and raw matrices for `skill`, or None if neither exists."""
    base = root or HARBOR_DIR
    found = [p for p in (base / f"{skill}.rescored.json", base / f"{skill}.json") if p.is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def _split(combo: str) -> tuple[str, str]:
    harness, _, model = combo.partition("@")
    return harness, model or "harness default"


def read_matrix(skill: str, root: Path | None = None) -> dict | None:
    """Normalized rows for one skill, or None when the skill has never been run.

    Raises ValueError on an unreadable or malformed file rather than returning an empty matrix: a
    page saying "no combinations" when the file is actually corrupt reads as "nothing helps"."""
    path = matrix_path(skill, root)
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{path.name} is unreadable ({error})") from error
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} does not contain a matrix")

    source = raw.get("combinations") if isinstance(raw.get("combinations"), dict) else raw.get("harnesses")
    rows = []
    for combo, record in sorted((source or {}).items()):
        if not isinstance(record, dict):
            continue
        harness, model = _split(combo)
        target = TARGETS.get(str(record.get("target_alias") or ""), {})
        family = str(record.get("family") or "")
        display_model = (target.get("display_name")
                         if family.startswith("Qwen") and target.get("family") == family else None)
        row = {"combination": combo,
               "harness": record.get("harness") or harness,
               # Keep wire IDs in provenance and job identity, but present the canonical model
               # name whenever a pinned target alias provides one.
               "model": display_model or record.get("model") or model}
        # Endpoint identity is evidence, not presentation decoration. Legacy matrices do not
        # have it, so only copy fields that the row actually recorded; a fabricated null identity
        # would falsely suggest that an endpoint was checked.
        for field in ("target_alias", "endpoint_fingerprint", "protocol", "family",
                      "quantization", "tool_parser", "exploratory",
                      "rankable"):
            if field in record:
                row[field] = record[field]
        size = record.get("parameter_billions")
        if isinstance(size, (int, float)) and not isinstance(size, bool) and math.isfinite(size) \
                and size > 0:
            row["parameter_billions"] = size
        if "lift" not in record:
            # No lift, no means, no scores. A row shaped like a measurement is how "the container
            # died" became "lift +0.750" the first time this ran.
            row["error"] = str(record.get("error") or "not measured")
            rows.append(row)
            continue
        row.update({"lift": record["lift"],
                    "skill_mean": record.get("skill_mean"),
                    "control_mean": record.get("control_mean"),
                    "n": record.get("tasks_scored"),
                    "attempts": record.get("attempts") or 1,
                    "dropped": record.get("tasks_dropped") or []})
        rows.append(row)

    summary = summarize(rows)
    # A global winner is not meaningful once columns have independent evidence fitness. Keep the
    # reusable `summarize` result intact for callers that need it, but do not expose its global
    # `best` to the UI alongside per-model winners.
    summary.pop("best", None)
    models = sorted({row["model"] for row in rows})
    harnesses = sorted({row["harness"] for row in rows})
    exploratory = raw.get("exploratory") is True or any(
        row.get("exploratory") is True for row in rows)
    rankable = raw.get("rankable") is not False and all(
        row.get("rankable") is not False for row in rows)
    return {"skill": skill, "source": path.name, "rescored": path.name.endswith(".rescored.json"),
            "generated": int(path.stat().st_mtime), "judge": raw.get("judge") or "",
            "exploratory": exploratory, "rankable": rankable,
            "rows": rows, "models": models, "harnesses": harnesses,
            "model_summaries": summarize_models(rows), **summary}


def summarize(rows: list[dict]) -> dict:
    """Headline numbers, plus whether the matrix is fit to be read as a ranking at all."""
    measured = [r for r in rows if "lift" in r]
    if not measured:
        return {"measured": 0, "unmeasured": len(rows), "mean_lift": None,
                "control_mean": None, "best": None,
                "warning": "No combination produced a measurement." if rows else ""}

    controls = [r["control_mean"] for r in measured if isinstance(r.get("control_mean"), (int, float))]
    control_mean = sum(controls) / len(controls) if controls else None
    best = max(measured, key=lambda r: r["lift"])

    warning = ""
    if any(r.get("rankable") is False for r in measured):
        warning = ("Exploratory evidence is shown for inspection but is not readable as a "
                   "ranking. Re-run with the full measurement contract before naming a winner.")
    elif control_mean is not None and control_mean >= CEILING:
        # Deliberately not "the best combination is X". At this control level the ranking is noise,
        # and naming a winner off it is the failure mode this whole module exists to prevent.
        warning = (f"Controls average {control_mean:.3f} of 1.00, at or above the {CEILING:.2f} "
                   f"ceiling. There is less headroom left than the judge's own run-to-run spread, "
                   f"so these combinations cannot be ranked against each other — the held-out tasks "
                   f"are too easy, whatever the lift column says.")
    elif all((r.get("n") or 0) < 3 for r in measured):
        warning = ("Every combination was measured over fewer than 3 tasks; these differences are "
                   "not distinguishable from noise.")
    elif all((r.get("attempts") or 1) < 2 for r in measured):
        # Measured, not assumed: two control-arm runs of an identical configuration moved a task's
        # score by 0.278 and swapped two harnesses' rank, while re-judging one fixed answer three
        # times was identical. A single attempt per task sits under that, so a ranking built from
        # these rows is a ranking of the agents' own run-to-run variance.
        warning = ("Every combination was run with one attempt per task. Repeat runs of an "
                   "identical configuration have moved a task's score by 0.28 and swapped the rank "
                   "of two harnesses, so single-attempt differences are agent variance rather than "
                   "an effect. Re-run with -k 3 or more before reading this as a ranking.")

    return {"measured": len(measured), "unmeasured": len(rows) - len(measured),
            "mean_lift": sum(r["lift"] for r in measured) / len(measured),
            "control_mean": control_mean,
            "best": None if warning else {"combination": best["combination"], "lift": best["lift"],
                                          "n": best.get("n")},
            "warning": warning}


def summarize_models(rows: list[dict]) -> dict[str, dict]:
    """Summarize each recorded model column without filling absent intersections."""
    summaries = {}
    for model in sorted({row["model"] for row in rows}):
        summary = summarize([row for row in rows if row["model"] == model])
        best = summary.pop("best")
        summaries[model] = {
            "model": model,
            "measured": summary["measured"],
            "unmeasured": summary["unmeasured"],
            "mean_lift": summary["mean_lift"],
            "control_mean": summary["control_mean"],
            "warning": summary["warning"],
            "best_harness": None if best is None else next(
                row["harness"] for row in rows
                if row["model"] == model and row.get("combination") == best["combination"]
            ),
        }
    return summaries


def available(root: Path | None = None) -> list[str]:
    """Skills that have a matrix on disk, newest first."""
    base = root or HARBOR_DIR
    if not base.is_dir():
        return []
    skills = {p.name.split(".")[0] for p in base.glob("*.json")}
    return sorted((s for s in skills if s), key=lambda s: -(matrix_path(s, base).stat().st_mtime))
