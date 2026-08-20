"""Durable skill-catalog caller for :func:`ingot.optimize.harbor_eval.run_local_sweep`.

One controller owns this filesystem queue. Each item invokes the restart-safe native Harbor
one-skill path; Harbor trial, telemetry, grade, and publication receipts remain the source of truth.
The catalog files schedule those calls and never duplicate their evidence state.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import yaml

from ingot.mcp_server.registry import load_skills, skill_revision
from ingot.optimize import resolve_skill_dir
from ingot.optimize.ab import TASKS_DIR
from ingot.optimize.harbor_eval import HARBOR_DIR, SCORING_REVISION, _task_fingerprint, run_local_sweep
from ingot.optimize.harbor_native import NATIVE_RUNNER_REVISION, native_trial_identity
from ingot.optimize.harbor_targets import HARNESS_PROTOCOLS, LocalTarget, discover_target, parse_target


_SCHEMA = 1
CATALOG_OWNER = HARBOR_DIR / "catalog.controller.lock"


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class CatalogIntent:
    skill: str
    skill_sha256: str
    task_fingerprint: str
    target_specs: tuple[str, ...]
    target_fingerprints: tuple[str, ...]
    harnesses: tuple[str, ...]
    runtime_revisions: tuple[tuple[str, str], ...]
    publish_root: str = str(HARBOR_DIR)
    global_concurrency: int = 16
    endpoint_concurrency: int = 2
    priority: int = 100

    def __post_init__(self) -> None:
        if not self.skill or len(self.skill_sha256) != 64 or len(self.task_fingerprint) != 64:
            raise ValueError("catalog intent requires skill and full SHA-256 identities")
        if not self.target_specs or len(self.target_specs) != len(self.target_fingerprints):
            raise ValueError("catalog intent target specs and fingerprints must align")
        if not self.harnesses or any(item not in HARNESS_PROTOCOLS for item in self.harnesses):
            raise ValueError("catalog intent contains no harnesses or an unknown harness")
        if len(set(self.target_specs)) != len(self.target_specs) or len(set(self.harnesses)) != len(self.harnesses):
            raise ValueError("catalog intent contains duplicate targets or harnesses")
        if (self.global_concurrency < 1 or self.endpoint_concurrency < 1
                or self.endpoint_concurrency > self.global_concurrency):
            raise ValueError("catalog concurrency requires 1 <= endpoint <= global")
        if not self.publish_root or not Path(self.publish_root).is_absolute():
            raise ValueError("catalog publish root must be an absolute path")

    def identity_payload(self) -> dict:
        payload = asdict(self)
        payload.pop("priority")
        payload["target_specs"] = list(self.target_specs)
        payload["target_fingerprints"] = list(self.target_fingerprints)
        payload["harnesses"] = list(self.harnesses)
        payload["runtime_revisions"] = [list(item) for item in self.runtime_revisions]
        return payload

    @property
    def digest(self) -> str:
        encoded = json.dumps(self.identity_payload(), sort_keys=True,
                             separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def _heldout(skill: str) -> list[dict] | None:
    path = TASKS_DIR / f"{skill}.yaml"
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        return None
    holdout = data.get("holdout") or data.get("train") or data.get("tasks") or []
    return holdout if isinstance(holdout, list) and holdout else None


def _skill_sha(skill: str) -> str:
    return hashlib.sha256((resolve_skill_dir(skill) / "SKILL.md").read_bytes()).hexdigest()


def _intent_for_skill(skill: str, target_specs: Sequence[str], harnesses: Sequence[str],
                      *, priority: int = 100, global_concurrency: int = 16,
                      endpoint_concurrency: int = 2,
                      publish_root: Path | str = HARBOR_DIR) -> CatalogIntent | None:
    provisional = tuple(parse_target(spec) for spec in target_specs)
    targets = tuple(discover_target(target.alias, target.base_url) for target in provisional)
    holdout = _heldout(skill)
    if not holdout:
        return None
    source = resolve_skill_dir(skill)
    revisions = [("harbor", "0.20.0"), ("runner", NATIVE_RUNNER_REVISION),
                 ("scoring", SCORING_REVISION), ("skill-tree", skill_revision(source))]
    revisions.extend(
        (f"route:{target.fingerprint}:{harness}",
         f"{native_trial_identity(target, harness, 'skill').protocol}/"
         f"{native_trial_identity(target, harness, 'skill').gateway_revision}/"
         f"context={target.context_length}")
        for target in targets for harness in harnesses
    )
    return CatalogIntent(
        skill=skill,
        skill_sha256=_skill_sha(skill),
        task_fingerprint=_task_fingerprint(holdout),
        target_specs=tuple(target_specs),
        target_fingerprints=tuple(target.fingerprint for target in targets),
        harnesses=tuple(harnesses),
        runtime_revisions=tuple(revisions),
        publish_root=str(Path(publish_root)),
        global_concurrency=global_concurrency,
        endpoint_concurrency=endpoint_concurrency,
        priority=priority,
    )


def _intent_document(intent: CatalogIntent) -> dict:
    return {"schema": _SCHEMA, "digest": intent.digest, "identity": intent.identity_payload()}


def _state_path(root: Path, digest: str) -> Path:
    return root / "state" / f"{digest}.json"


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise RuntimeError(f"catalog record is not an object: {path}")
    return value


def enqueue_catalog(root: Path | str, intents: Iterable[CatalogIntent]) -> list[Path]:
    """Persist measurement intents; scheduling priority never changes content identity."""
    root = Path(root)
    intent_dir = root / "intents"
    known = []
    if intent_dir.is_dir():
        known = [_read_json(path) for path in intent_dir.glob("*.json")]
    written: list[Path] = []
    for supplied in intents:
        changed = any(item.get("identity", {}).get("skill") == supplied.skill
                      and item.get("identity", {}).get("skill_sha256") != supplied.skill_sha256
                      for item in known)
        path = intent_dir / f"{supplied.digest}.json"
        state_path = _state_path(root, supplied.digest)
        if not path.exists():
            _atomic_json(path, _intent_document(supplied))
            known.append(_intent_document(supplied))
        if state_path.exists():
            state = _read_json(state_path)
            if state.get("status") != "complete":
                if state.get("status") == "superseded":
                    state["status"] = "pending"
                    state.pop("error", None)
                    state.pop("finished_at", None)
                state["priority"] = max(int(state.get("priority", 0)),
                                        300 if changed else 200)
                _atomic_json(state_path, state)
        else:
            _atomic_json(state_path, {"schema": _SCHEMA, "intent_digest": supplied.digest,
                                      "status": "pending",
                                      "priority": max(supplied.priority, 300 if changed else 100)})
        written.append(path)
    return written


def _process_start_token(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[21]
    except (OSError, IndexError):
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                              text=True)
        return done.stdout.strip() or None


def _claim_controller(path: Path):
    """Hold one kernel lock across all catalog roots; no stale-receipt unlink race exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.seek(0)
        try:
            current = json.load(handle)
        except (ValueError, OSError):
            current = {}
        handle.close()
        raise RuntimeError(f"catalog already has live controller PID {current.get('pid', 'unknown')}") from error
    handle.seek(0)
    handle.truncate()
    json.dump({"schema": _SCHEMA, "pid": os.getpid(),
               "start_token": _process_start_token(os.getpid())}, handle, sort_keys=True)
    handle.flush()
    os.fsync(handle.fileno())
    return handle


def _load_intent(path: Path, priority: int) -> CatalogIntent:
    document = _read_json(path)
    identity = document.get("identity")
    if not isinstance(identity, dict) or document.get("digest") != path.stem:
        raise RuntimeError(f"catalog intent identity is invalid: {path}")
    revisions = tuple(tuple(item) for item in identity["runtime_revisions"])
    intent = CatalogIntent(**{**identity, "target_specs": tuple(identity["target_specs"]),
                              "target_fingerprints": tuple(identity["target_fingerprints"]),
                              "harnesses": tuple(identity["harnesses"]),
                              "runtime_revisions": revisions, "priority": priority})
    if intent.digest != path.stem or document.get("digest") != intent.digest:
        raise RuntimeError(f"catalog intent digest is invalid: {path}")
    return intent


def _prepare_execution(root: Path, intent: CatalogIntent) -> tuple[Path, Path]:
    execution_root = root / "runs" / intent.digest
    source = execution_root / "staged"
    staged_skill = source / intent.skill
    revisions = dict(intent.runtime_revisions)
    expected_tree = revisions.get("skill-tree")
    if not expected_tree:
        raise RuntimeError("catalog intent lacks full skill-tree revision")
    if not staged_skill.exists():
        execution_root.mkdir(parents=True, exist_ok=True)
        temporary = execution_root / f".staged.{os.getpid()}.tmp"
        shutil.rmtree(temporary, ignore_errors=True)
        try:
            copied = temporary / intent.skill
            shutil.copytree(resolve_skill_dir(intent.skill), copied)
            if skill_revision(copied) != expected_tree:
                raise RuntimeError(f"catalog skill tree changed while staging: {intent.skill}")
            os.replace(temporary, source)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
    if skill_revision(staged_skill) != expected_tree:
        raise RuntimeError(f"catalog staged skill tree identity changed: {intent.skill}")
    return source, execution_root


def _refuse_live_harbor(execution_root: Path) -> None:
    """Do not launch probes/canaries over a surviving Harbor child for this exact intent."""
    listing = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True,
                             check=True).stdout
    marker = str(execution_root)
    live = []
    for line in listing.splitlines():
        pid, separator, command = line.strip().partition(" ")
        if separator and pid.isdigit() and marker in command and "harbor run" in command:
            live.append(pid)
    if live:
        raise RuntimeError(f"catalog intent already has live Harbor child PID {','.join(live)}")


def run_catalog(root: Path | str, *, max_skills: int | None = None,
                stop_file: Path | str | None = None, controller_owner=None,
                process_env: Mapping[str, str] | None = None) -> None:
    """Run queued skills serially; native Harbor owns all within-skill parallelism."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    stop = Path(stop_file) if stop_file is not None else root / "STOP"
    owner = controller_owner or _claim_controller(CATALOG_OWNER)
    owns_controller = controller_owner is None
    attempted = 0
    try:
        candidates = []
        for state_path in (root / "state").glob("*.json") if (root / "state").is_dir() else ():
            state = _read_json(state_path)
            if state_path.stem != state.get("intent_digest"):
                raise RuntimeError(f"catalog state digest is invalid: {state_path}")
            if state.get("status") in {"pending", "running", "failed"}:
                candidates.append((int(state.get("priority", 0)), state_path, state))
        for _, state_path, state in sorted(candidates, key=lambda item: (-item[0], item[1].name)):
            if stop.exists() or (max_skills is not None and attempted >= max_skills):
                break
            digest = state_path.stem
            intent = _load_intent(root / "intents" / f"{digest}.json",
                                  int(state.get("priority", 0)))
            current = _intent_for_skill(intent.skill, intent.target_specs, intent.harnesses,
                                        priority=300,
                                        global_concurrency=intent.global_concurrency,
                                        endpoint_concurrency=intent.endpoint_concurrency,
                                        publish_root=intent.publish_root)
            if current is None:
                state.update(status="failed", finished_at=time.time(), error="MissingTasks")
                _atomic_json(state_path, state)
                attempted += 1
                continue
            if current.digest != intent.digest:
                enqueue_catalog(root, [current])
                state.update(status="superseded", finished_at=time.time(), error="IdentityChanged")
                _atomic_json(state_path, state)
                continue
            source, execution_root = _prepare_execution(root, intent)
            _refuse_live_harbor(execution_root)
            state.update(status="running", started_at=state.get("started_at") or time.time(),
                         run_root=str(execution_root))
            _atomic_json(state_path, state)
            attempted += 1
            try:
                targets: list[LocalTarget] = [parse_target(spec) for spec in intent.target_specs]
                manifest = run_local_sweep(
                    intent.skill, targets, harnesses=intent.harnesses, attempts=3,
                    native_parallel=True, skill_source=str(source), evidence_root=execution_root,
                    expected_task_fingerprint=intent.task_fingerprint,
                    expected_runtime_revisions=dict(intent.runtime_revisions),
                    global_concurrency=intent.global_concurrency,
                    endpoint_concurrency=intent.endpoint_concurrency,
                    publish_root=Path(intent.publish_root), content_addressed_resume=True,
                    process_env=process_env)
                if manifest.get("aborted"):
                    raise RuntimeError("sweep aborted")
                if manifest.get("telemetry_pending"):
                    raise RuntimeError("sweep telemetry is pending verification")
            except Exception as error:  # noqa: BLE001 - record one failed item, continue catalog
                state.update(status="failed", finished_at=time.time(), error=type(error).__name__)
            else:
                state.update(status="complete", finished_at=time.time(),
                             utilization=manifest.get("utilization"),
                             combinations=len(manifest.get("combinations", {})))
            _atomic_json(state_path, state)
    finally:
        if owns_controller:
            fcntl.flock(owner.fileno(), fcntl.LOCK_UN)
            owner.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Queue restart-safe native Harbor skill sweeps")
    parser.add_argument("--root", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--skill", action="append")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--target", action="append", required=True,
                        help="repeat ALIAS=BASE_URL")
    parser.add_argument("--harness", action="append", choices=tuple(HARNESS_PROTOCOLS),
                        default=[])
    parser.add_argument("--max-skills", type=int)
    parser.add_argument("--global-concurrency", type=int, default=16)
    parser.add_argument("--endpoint-concurrency", type=int, default=2)
    parser.add_argument("--publish-root", type=Path, default=HARBOR_DIR,
                        help="absolute directory consumed by the UI for final matrices")
    parser.add_argument("--enqueue-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    skills = [item.name for item in load_skills()] if args.all else args.skill
    harnesses = tuple(args.harness or HARNESS_PROTOCOLS)
    intents = [intent for skill in skills if (intent := _intent_for_skill(
        skill, args.target, harnesses, global_concurrency=args.global_concurrency,
        endpoint_concurrency=args.endpoint_concurrency,
        publish_root=args.publish_root)) is not None]
    enqueue_catalog(args.root, intents)
    if not args.enqueue_only:
        run_catalog(args.root, max_skills=args.max_skills)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the installed module CLI
    raise SystemExit(main())
