"""Recompute compatible Harbor matrices from job directories already on disk.

The agents have already run; rescoring only re-reads their persisted verifier artifacts.  A matrix
may combine the preserved proprietary jobs and endpoint-qualified local jobs only when the evidence
states that they used the same task set and retry count. Every answer is then judged under the
current scoring provenance.

Usage: python -m ingot.optimize.harbor_rescore <skill> [--jobs runs/harbor/jobs/<skill>]
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Sequence

from .ab import load_tasks
from .agy_judge import AGY_IDENTITY, preflight
from .harbor_eval import (HARBOR_DIR, _task_fingerprint, _task_name,
                          broken_tasks, broken_trials, collect_answers, score)
from .harbor_langfuse import load_provenance_metadata, validate_job_receipts
from .harbor_native import NATIVE_RUNNER_REVISION, NativeTrialIdentity, identity_from_env


_EVIDENCE_FIELDS = ("task_fingerprint", "attempts")
_CURRENT_SCORING = {
    "judge": "agy/gemini-3.6-flash-medium",
    "scoring_revision": "harbor-rubric-v2-agy",
    "judge_billing_mode": "subscription",
}
_HISTORICAL_SCORING_FIELDS = {
    "judge", "scoring_revision", "judge_runtime", "judge_billing_mode", "billing_mode",
    "cost_usd",
}
_MEASUREMENT_FIELDS = {
    "error", "score", "scores", "skill_mean", "control_mean", "lift", "skill_scores",
    "control_scores", "tasks_scored", "tasks_dropped", "mean_lift", "scored", "unscorable",
    "n", "dropped", "best", "measured", "unmeasured",
    "canary_error",
}
_AGY_SCORE_CONCURRENCY = 4


def _arm_evidence(combo: Path, arm: str) -> tuple[Path, NativeTrialIdentity | None]:
    """Resolve legacy arm directories or one exact arm view inside a native sibling job."""
    metadata = _combo_identity(combo)
    native_jobs = metadata.get("native_jobs")
    if native_jobs is not None and not isinstance(native_jobs, dict):
        raise ValueError("native jobs must map arms to sibling slugs")
    native_job = native_jobs.get(arm) if isinstance(native_jobs, dict) else metadata.get("native_job")
    if native_job is None:
        return combo / arm, None
    if (not isinstance(native_job, str)
            or not native_job
            or not all(character.isalnum() or character in "._-" for character in native_job)):
        raise ValueError("native job must be a safe sibling slug")
    identities = metadata.get("native_identities")
    if not isinstance(identities, dict) or arm not in identities:
        raise ValueError(f"native job is missing {arm} identity")
    env = identities[arm]
    if not isinstance(env, dict):
        raise ValueError(f"native job {arm} identity is invalid")
    identity = identity_from_env(env)
    if identity.arm != arm:
        raise ValueError(f"native job {arm} identity has the wrong arm")
    expected = {
        "combination_id": metadata.get("combination"),
        "harness": metadata.get("harness"),
        "endpoint_fingerprint": metadata.get("endpoint_fingerprint"),
        "protocol": metadata.get("protocol"),
        "gateway_revision": metadata.get("gateway_revision", "direct"),
    }
    for field, value in expected.items():
        if getattr(identity, field) != value:
            raise ValueError(f"native job {arm} identity does not match combo metadata")
    return combo.parent / native_job, identity


def _combo_identity(combo: Path) -> dict:
    """Return every identity field the live run recorded beside this combination."""
    try:
        record = json.loads((combo / "combo.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{combo} has no readable combo.json identity") from error
    if not isinstance(record, dict) or not record:
        raise ValueError(f"{combo} has no combo identity")
    return dict(record)


def _identity_key(identity: dict) -> str:
    """Deduplicate exact recorded identities, never lossy job-directory names."""
    try:
        return json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ValueError("combo identity is not JSON data") from error


def discover_combinations(roots: Sequence[Path]) -> list[Path]:
    """Return unique combinations across roots, keyed by complete combo metadata."""
    found: list[Path] = []
    identities: set[str] = set()
    for root in roots:
        if not root.is_dir():
            raise SystemExit(f"no job directories at {root}")
        combos = sorted(path for path in root.iterdir()
                        if path.is_dir() and (path / "combo.json").is_file())
        for combo in combos:
            record = _combo_identity(combo)
            key = _identity_key(record)
            if key not in identities:
                found.append(combo)
                identities.add(key)
    return found


def select_combinations(roots: Sequence[Path], paths: Sequence[Path]) -> list[Path]:
    """Select exact real directories without inspecting sibling combinations."""
    allowed = {root.resolve() for root in roots}
    found: list[Path] = []
    identities: set[str] = set()
    for path in paths:
        if path.is_symlink() or not path.is_dir() or path.parent.resolve() not in allowed:
            raise ValueError(f"selected combination must be a real immediate child of a jobs root: {path}")
        resolved = path.resolve()
        key = _identity_key(_combo_identity(resolved))
        if key not in identities:
            found.append(resolved)
            identities.add(key)
    if not found:
        raise ValueError("no completed combination paths selected")
    return found


def validate_compatibility(records: Sequence[dict]) -> None:
    """Refuse raw evidence whose task set or retry count differs."""
    if not records:
        raise ValueError("no combination evidence found")
    for field in _EVIDENCE_FIELDS:
        values = [record.get(field) for record in records]
        if field == "attempts":
            invalid = any(not isinstance(value, int) or isinstance(value, bool) or value < 1
                          for value in values)
        else:
            invalid = any(not isinstance(value, str) or not value.strip() for value in values)
        if invalid:
            raise ValueError(f"combination metadata is missing or invalid {field}")
        if len(set(values)) != 1:
            raise ValueError(f"incompatible combination metadata: {field}")


def _validate_current_compatibility(records: Sequence[dict], holdout: list[dict]) -> None:
    """Require otherwise-compatible evidence to describe this exact raw task run."""
    if records[0]["task_fingerprint"] != _task_fingerprint(holdout):
        raise ValueError("combination metadata does not match current task_fingerprint")
    attempts = records[0]["attempts"]
    exploratory = all(record.get("exploratory") is True and record.get("rankable") is False
                      for record in records)
    if attempts != 3 and not (attempts == 1 and exploratory):
        raise ValueError("combination metadata attempts do not match current measurement contract")


def _load_legacy_manifest(path: Path) -> dict:
    """Read the user-supplied historical metadata; never fill missing history from today."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"legacy metadata manifest is unreadable: {path}") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("root"), str):
        raise ValueError("legacy metadata manifest needs a root")
    missing = [field for field in _EVIDENCE_FIELDS if field not in manifest]
    if missing:
        raise ValueError(f"legacy metadata manifest is missing {', '.join(missing)}")
    return manifest


def _legacy_metadata(skill: str, root: Path, combo: Path, identity: dict,
                     manifest: dict | None) -> dict:
    """Attach only explicit history to an old proprietary combination after cross-checking it."""
    if manifest is None:
        raise ValueError(f"{combo} needs an explicit legacy metadata manifest")
    if Path(manifest["root"]).resolve() != root.resolve():
        raise ValueError(f"{combo} legacy metadata manifest names a different root")
    try:
        matrix = json.loads((HARBOR_DIR / f"{skill}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{combo} lacks explicit matrix metadata") from error
    if not isinstance(matrix, dict):
        raise ValueError(f"{combo} lacks explicit matrix metadata")

    key = identity.get("combination") or combo.name
    rows = matrix.get("harnesses") or {}
    row = rows.get(key) if isinstance(rows, dict) else None
    if matrix.get("skill") != skill or not isinstance(row, dict):
        raise ValueError(f"{combo} lacks explicit matrix metadata")
    metadata = {field: manifest[field] for field in _EVIDENCE_FIELDS}
    matrix_checks = {"attempts": row.get("attempts")}
    conflicting_matrix = [field for field, value in matrix_checks.items()
                          if value not in (None, "") and value != metadata[field]]
    if conflicting_matrix:
        raise ValueError(f"{combo} conflicts with legacy matrix metadata: {', '.join(conflicting_matrix)}")
    conflicting = [field for field in _EVIDENCE_FIELDS
                   if identity.get(field) not in (None, "")
                   and identity[field] != metadata[field]]
    if conflicting:
        raise ValueError(f"{combo} conflicts with legacy metadata manifest: {', '.join(conflicting)}")
    return {**identity, **metadata}


def _evidence_metadata(skill: str, root: Path, combo: Path, manifest: dict | None) -> dict:
    identity = _combo_identity(combo)
    if all(identity.get(field) not in (None, "") for field in _EVIDENCE_FIELDS):
        return identity
    return _legacy_metadata(skill, root, combo, identity, manifest)


def _current_scoring(runtime: dict) -> dict:
    """Scoring provenance for this one rescore, without inventing metered cost."""
    return {**_CURRENT_SCORING, "judge_runtime": str(runtime["version"])}


def current_scoring_identity() -> dict:
    """Resolve the exact current subscription scorer identity once for a controller run."""
    return _current_scoring(preflight())


def _output_identity(identity: dict, scoring: dict) -> dict:
    """Replace any historical scoring receipt while preserving raw evidence identity."""
    raw = {key: value for key, value in identity.items()
           if key not in _HISTORICAL_SCORING_FIELDS | _MEASUREMENT_FIELDS}
    return {**raw, **scoring}


def _validate_attempt_artifacts(arm: Path, skill: str, holdout: list[dict], attempts: int,
                                identity: NativeTrialIdentity | None = None) -> None:
    """Require one readable Harbor trial record for every expected task attempt."""
    observed = {_task_name(skill, index): 0 for index in range(len(holdout))}
    from .harbor_native import iter_attempt_dirs
    results = ([attempt / "result.json" for attempt in iter_attempt_dirs(arm, identity=identity)]
               if identity is not None else sorted(arm.glob("*/result.json")))
    for result in results:
        try:
            record = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError(f"{arm.name} arm has an unreadable attempt record") from error
        task_name = record.get("task_name") if isinstance(record, dict) else None
        if not isinstance(task_name, str) or not task_name.strip():
            raise RuntimeError(f"{arm.name} arm has an attempt record without a task name")
        task = task_name.split("/")[-1].split("__")[0]
        if task not in observed:
            raise RuntimeError(f"{arm.name} arm has an unexpected attempt for {task}")
        observed[task] += 1
    mismatched = [f"{task}={count}" for task, count in observed.items() if count != attempts]
    if mismatched:
        raise RuntimeError(f"{arm.name} arm needs exactly {attempts} attempt records per task; "
                           f"found {', '.join(mismatched)}")


def _row_key(identity: dict, rows: dict[str, dict]) -> str:
    """Keep a malformed repeated combination label from overwriting distinct endpoint evidence."""
    key = str(identity.get("combination") or "")
    if not key:
        raise ValueError("combo identity is missing combination")
    if key not in rows:
        return key
    fingerprint = str(identity.get("endpoint_fingerprint") or "unknown")
    qualified = f"{key}--{fingerprint}"
    if qualified not in rows:
        return qualified
    suffix = 2
    while f"{qualified}-{suffix}" in rows:
        suffix += 1
    return f"{qualified}-{suffix}"


def atomic_write_json(path: Path, payload: dict) -> None:
    """Publish JSON with one replacement; every earlier failure leaves ``path`` untouched."""
    encoded = json.dumps(payload, indent=2).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _summary(skill: str, rows: dict[str, dict], shared: dict, scoring: dict) -> dict | None:
    scored = {name: row for name, row in rows.items() if "lift" in row}
    if not scored:
        return None
    return {
        "skill": skill,
        "combinations": rows,
        "exploratory": any(row.get("exploratory") is True for row in rows.values()),
        "rankable": all(row.get("rankable") is not False for row in rows.values()),
        "mean_lift": sum(row["lift"] for row in scored.values()) / len(scored),
        "scored": len(scored),
        "unscorable": len(rows) - len(scored),
        **shared,
        **scoring,
    }


def _prior_rows(path: Path, skill: str, shared: dict, scoring: dict,
                skill_sha256: str) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"incompatible existing rescore output: {path}") from error
    expected = {"skill": skill, **shared, **scoring}
    if (not isinstance(payload, dict)
            or any(payload.get(key) != value for key, value in expected.items())
            or not isinstance(payload.get("combinations"), dict)):
        raise ValueError(f"incompatible existing rescore output: {path}")
    rows = payload["combinations"]
    if any(not isinstance(key, str) or not isinstance(row, dict) for key, row in rows.items()):
        raise ValueError(f"incompatible existing rescore output: {path}")
    return {key: row for key, row in rows.items() if row.get("skill_sha256") == skill_sha256}


def _matching_row_key(identity: dict, rows: dict[str, dict]) -> str | None:
    fields = ("combination", "harness", "model", "target_alias", "endpoint_fingerprint",
              "protocol", "task_fingerprint", "skill_sha256", "attempts",
              "gateway_revision", "gateway_identity", "family", "parameter_billions",
              "quantization", "tool_parser")
    for key, row in rows.items():
        if all(row.get(field) == identity.get(field) for field in fields):
            return key
    return None


def _same_logical_cell(left: dict, right: dict) -> bool:
    fields = ("combination", "harness", "target_alias", "endpoint_fingerprint", "protocol",
              "task_fingerprint", "skill_sha256", "attempts")
    return all(left.get(field) == right.get(field) for field in fields)


def _prefer_current_native_evidence(evidence: list[tuple[Path, dict]]) -> list[tuple[Path, dict]]:
    current = [identity for _, identity in evidence
               if str(identity.get("gateway_revision") or "").endswith(
                   f"-{NATIVE_RUNNER_REVISION}")]
    if not current:
        return evidence
    return [
        item for item in evidence if (
            str(item[1].get("gateway_revision") or "").endswith(f"-{NATIVE_RUNNER_REVISION}")
            or not any(_same_logical_cell(item[1], identity) for identity in current)
        )
    ]


def rescore(skill: str, jobs_roots: Sequence[Path] | None = None,
            legacy_metadata: Path | None = None,
            provenance_metadata: Sequence[Path] | None = None,
            combination_paths: Sequence[Path] | None = None,
            output: Path | None = None, scoring_identity: dict | None = None, log=print) -> dict:
    """Recompute compatible combinations and atomically publish their matrix when measured."""
    if os.environ.get("JUDGE_BACKEND", "").strip().lower() != "agy":
        raise RuntimeError("Harbor rescore requires JUDGE_BACKEND=agy; refusing scorer fallback")
    selected_mode = combination_paths is not None
    roots = list(jobs_roots) if jobs_roots is not None else [HARBOR_DIR / "jobs" / skill]
    provenance_paths = list(provenance_metadata or [])
    if provenance_paths and len(provenance_paths) != len(roots):
        raise ValueError("each jobs root requires one paired provenance metadata document")
    provenance_by_root = {
        root.resolve(): load_provenance_metadata(path)
        for root, path in zip(roots, provenance_paths)
    }
    _, holdout, _ = load_tasks(skill)
    manifest = _load_legacy_manifest(legacy_metadata) if legacy_metadata else None
    discovered = (select_combinations(roots, combination_paths or [])
                  if selected_mode else discover_combinations(roots))
    evidence = _prefer_current_native_evidence([
        (combo, _evidence_metadata(skill, combo.parent, combo, manifest))
        for combo in discovered
    ])
    records = [identity for _, identity in evidence]
    validate_compatibility(records)
    _validate_current_compatibility(records, holdout)
    skill_sha256 = ""
    if selected_mode:
        skill_revisions = {identity.get("skill_sha256") for identity in records}
        if (len(skill_revisions) != 1 or not isinstance(next(iter(skill_revisions)), str)
                or not next(iter(skill_revisions)).strip()):
            raise ValueError("selected combinations need one non-empty skill_sha256")
        skill_sha256 = next(iter(skill_revisions))
        for combo, identity in evidence:
            if identity.get("canary_error") or identity.get("measurement_error"):
                continue
            for arm in ("skill", "control"):
                job, arm_identity = _arm_evidence(combo, arm)
                if not job.is_dir():
                    raise RuntimeError(f"selected combination is incomplete (missing {arm} arm): {combo}")
                _validate_attempt_artifacts(job, skill, holdout, identity["attempts"], arm_identity)
    for combo, identity in evidence:
        if identity.get("canary_error") or identity.get("measurement_error"):
            continue
        receipt_metadata = {
            **_combo_identity(combo),
            **provenance_by_root.get(combo.parent.resolve(), {}),
        }
        for arm in ("skill", "control"):
            job, arm_identity = _arm_evidence(combo, arm)
            if job.is_dir():
                validate_job_receipts(job, {**receipt_metadata, "arm": arm},
                                      identity=arm_identity)
    scoring = dict(scoring_identity or current_scoring_identity())
    shared = {field: evidence[0][1][field] for field in _EVIDENCE_FIELDS}
    out = output or HARBOR_DIR / f"{skill}.rescored.json"

    rows = (_prior_rows(out, skill, shared, scoring, skill_sha256) if selected_mode else {})
    last_checkpoint = None
    for combo, identity in evidence:
        matching = _matching_row_key(identity, rows)
        for prior_key, prior_row in list(rows.items()):
            if prior_key != matching and _same_logical_cell(identity, prior_row):
                del rows[prior_key]
        key = matching or _row_key(identity, rows)
        # Selected progressive runs carry earlier paths so pre-measurement failures can enter the
        # first published matrix. Compatible prior lifts are durable grade receipts, not an order
        # to pay Agy again or replace a stochastic score.
        output_identity = _output_identity(identity, scoring)
        if identity.get("canary_error") or identity.get("measurement_error"):
            error = str(identity.get("canary_error") or identity["measurement_error"])[:300]
            rows[key] = {**output_identity, "error": error}
            log(f"[rescore] {key:<44} {error}")
            continue
        if "lift" in rows.get(key, {}):
            continue
        skill_arm, skill_identity = _arm_evidence(combo, "skill")
        control_arm, control_identity = _arm_evidence(combo, "control")
        if not (skill_arm.is_dir() and control_arm.is_dir()):
            error = str(identity.get("canary_error") or identity.get("measurement_error")
                        or "incomplete (missing an arm)")[:300]
            if "lift" not in rows.get(key, {}):
                rows[key] = {**output_identity, "error": error}
            log(f"[rescore] {key:<44} {error}")
            continue
        try:
            _validate_attempt_artifacts(skill_arm, skill, holdout, identity["attempts"],
                                        skill_identity)
            _validate_attempt_artifacts(control_arm, skill, holdout, identity["attempts"],
                                        control_identity)
            skipped = (broken_tasks(skill_arm, skill_identity)
                       | broken_tasks(control_arm, control_identity))
            arms = {
                "skill": score(collect_answers(
                    skill_arm, broken_trials(skill_arm, skill_identity), identity=skill_identity),
                    skill, holdout, skipped, _AGY_SCORE_CONCURRENCY),
                "control": score(collect_answers(
                    control_arm, broken_trials(control_arm, control_identity), identity=control_identity),
                    skill, holdout, skipped, _AGY_SCORE_CONCURRENCY),
            }
        except RuntimeError as error:
            if "lift" not in rows.get(key, {}):
                rows[key] = {**output_identity, "error": str(error)[:300]}
            log(f"[rescore] {key:<44} UNSCORABLE: {str(error)[:90]}")
            continue
        skill_mean = sum(arms["skill"]) / len(arms["skill"])
        control_mean = sum(arms["control"]) / len(arms["control"])
        lift = skill_mean - control_mean
        verdict = "helps" if lift > 0.05 else "no lift" if lift >= -0.05 else "HURTS"
        note = f"  [{len(skipped)} dropped]" if skipped else ""
        log(f"[rescore] {key:<44} skill {skill_mean:.3f}  control {control_mean:.3f}  "
            f"lift {lift:+.3f}  ({verdict})  n={len(arms['skill'])}{note}")
        rows[key] = {
            **output_identity,
            "skill_mean": skill_mean,
            "control_mean": control_mean,
            "lift": lift,
            "skill_scores": arms["skill"],
            "control_scores": arms["control"],
            "tasks_scored": len(arms["skill"]),
            "tasks_dropped": sorted(skipped),
        }
        checkpoint = _summary(skill, rows, shared, scoring)
        if checkpoint is not None:
            atomic_write_json(out, checkpoint)
            last_checkpoint = checkpoint

    summary = _summary(skill, rows, shared, scoring)
    if summary is None:
        log("[rescore] nothing scorable; no matrix published")
        raise SystemExit(f"[rescore] no combination for '{skill}' was measured; nothing was measured.")
    if summary != last_checkpoint:
        atomic_write_json(out, summary)
    log(f"[rescore] {summary['scored']} scorable of {len(rows)}; "
        f"mean lift {summary['mean_lift']:+.4f}")
    log(f"[rescore] written to {out}")
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill")
    parser.add_argument("--jobs", action="append", default=None, metavar="PATH",
                        help="job root (repeatable; default runs/harbor/jobs/<skill>)")
    parser.add_argument("--legacy-metadata", type=Path, default=None, metavar="PATH",
                        help="authoritative metadata manifest for preserved proprietary jobs")
    parser.add_argument("--provenance-metadata", action="append", type=Path, default=None,
                        metavar="PATH",
                        help="provenance migration JSON paired with each repeated --jobs root")
    parser.add_argument("--combination-path", action="append", type=Path, default=None,
                        metavar="PATH", help="exact completed combination directory (repeatable)")
    parser.add_argument("--output", type=Path, default=None, metavar="PATH",
                        help="atomic matrix destination (default runs/harbor/<skill>.rescored.json)")
    args = parser.parse_args()
    rescore(
        args.skill, [Path(root) for root in args.jobs] if args.jobs else None,
        legacy_metadata=args.legacy_metadata,
        provenance_metadata=args.provenance_metadata,
        combination_paths=args.combination_path,
        output=args.output,
    )
