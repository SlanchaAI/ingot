"""Cross-model skill compatibility, how well a skill's body transfers across serving models.

A skill body is tuned for one serving model (`AGENT_MODEL`); SkillOpt's own result is that good
skills transfer, but not always. For each model in `COMPAT_MODELS`, this runs the skill's held-out
tasks through the one serving contract twice, once with the skill body, once with an empty body
(the no-skill baseline), judges both with the FIXED judge, and reports per-model **lift**
(skill mean − baseline mean). Positive lift = the body helps that model; ~0 = the model already
knows this and the body is dead weight there.

Langfuse-free: it reuses the direct rollout + judge (the same path the inner loop uses), so it needs
no trace backend or experiment logging. Only the *serving* model varies, the judge stays fixed so
scores are comparable across models.

Usage:  python -m ingot.optimize.compat <skill>
Config: COMPAT_MODELS=qwen/qwen3-32b,openai/gpt-5.5,anthropic/claude-sonnet-...  (default: AGENT_MODEL)
"""
import hashlib
import json
import os
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from langchain_openai import ChatOpenAI

from ingot.mcp_server.registry import optimizable_components

from . import (SERVE_TEMPLATE, agent_model, api_key, client_kwargs, configured_models,
               model_api_key, model_base_url, resolve_skill_dir, teacher_base_url)
from . import usage as usage_ledger
from .ab import load_tasks
from .judge import invoke_retry, judge
from .rollout import assemble
from ingot import paths

_MAX_WORKERS = 8
COMPAT_DIR = paths.runs() / "compat"
BASELINE_CACHE_DIR = paths.runs() / "compat-baseline"
# The no-skill baseline: the identical serving contract with no skill body, so `lift` isolates the
# body's contribution rather than the difference between two different prompts.
NO_SKILL_BODY = "(no skill loaded, answer the task from your own knowledge)"


def compat_models() -> list[str]:
    """Models to sweep: COMPAT_MODELS (comma-separated), else just the configured AGENT_MODEL."""
    models = configured_models("COMPAT_MODELS")
    return models or [agent_model()]


def _llm(model: str):
    # Route each row to the endpoint that actually serves it. The model this box serves
    # (AGENT_MODEL) comes from MODEL_BASE_URL — a local vLLM, and therefore a free row in the grid;
    # every other slug goes to the hosted endpoint. Sending the whole sweep to one endpoint meant
    # either the local row was impossible, or MODEL_BASE_URL had to be overridden by hand for the
    # run, which loses the free reference row exactly when you want to compare against it.
    # reasoning is left at the provider default, some models reject the flag.
    if model == agent_model():
        base, key = model_base_url(), model_api_key()
    else:
        base, key = teacher_base_url(), api_key()
    return ChatOpenAI(model=model, temperature=0, **client_kwargs(base, key=key))


def _baseline_cache_key(model: str, holdout: list[dict]) -> str:
    """The no-skill baseline is a pure function of (serving model, held-out tasks, judge): the skill
    body is precisely what it leaves out, so editing the skill cannot change it. Uncached, every
    re-sweep paid again for byte-identical work — half the cost of every run after the first."""
    payload = json.dumps({"v": 1, "model": model, "holdout": holdout,
                          "judge": os.environ.get("JUDGE_MODELS") or os.environ.get("JUDGE_MODEL", "")},
                         sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _score(llm, system: str, task: dict, role: str = "compat") -> float:
    msg = invoke_retry(llm, [("system", system), ("user", task["task"])])
    usage_ledger.add(role, getattr(msg, "usage_metadata", None))
    return judge(task["task"], task["rubric"], msg.content,
                 check=task.get("check"), deliverable=task.get("deliverable"),
                 checklist=task.get("checklist"))["score"]


def _run_arm(llm, system: str, tasks: list[dict], role: str = "compat") -> list[float]:
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(tasks))) as pool:
        return list(pool.map(lambda t: _score(llm, system, t, role), tasks))


def _sweep_model(model: str, skill_system: str, base_system: str, holdout: list[dict],
                 log=print) -> dict:
    """One row of the matrix: this model with the skill body, and without it."""
    llm = _llm(model)
    # Bill each arm to its own model: one "compat" bucket cannot be priced, because the whole
    # point of the sweep is that the serving model changes underneath it.
    role = f"compat:{model}"
    skill_scores = _run_arm(llm, skill_system, holdout, role)
    cache_path = BASELINE_CACHE_DIR / f"{_baseline_cache_key(model, holdout)}.json"
    if cache_path.exists():
        base_scores = json.loads(cache_path.read_text())
        log(f"[compat] {model:<34} baseline reused from cache (no spend)")
    else:
        base_scores = _run_arm(llm, base_system, holdout, role)
        BASELINE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(base_scores))
    s_mean, b_mean = statistics.mean(skill_scores), statistics.mean(base_scores)
    verdict = "helps" if s_mean - b_mean > 0.05 else "no lift" if s_mean - b_mean >= -0.05 else "HURTS"
    log(f"[compat] {model:<34} skill {s_mean:.3f}  baseline {b_mean:.3f}  "
        f"lift {s_mean - b_mean:+.3f}  ({verdict})")
    return {"skill_mean": s_mean, "baseline_mean": b_mean, "lift": s_mean - b_mean,
            "skill_scores": skill_scores, "baseline_scores": base_scores}


def run_compat(skill: str, log=print) -> dict:
    """Sweep COMPAT_MODELS over the skill's held-out tasks (skill vs no-skill) and write the matrix
    to runs/compat/<skill>.json. Returns the summary."""
    usage_ledger.reset()
    skill_dir = resolve_skill_dir(skill)
    _, holdout, _ = load_tasks(skill)
    if not holdout:
        raise SystemExit(f"'{skill}' has no held-out eval tasks to run.")
    skill_system = SERVE_TEMPLATE.format(body=assemble(optimizable_components(skill_dir)))
    base_system = SERVE_TEMPLATE.format(body=NO_SKILL_BODY)
    models = compat_models()
    log(f"[compat] '{skill}': {len(holdout)} held-out tasks × {len(models)} model(s); "
        f"judge fixed, serving model varies")

    models_out = {}
    for model in models:
        # One model the endpoint cannot serve must not discard the rows already paid for. A slug
        # with no ZDR-qualified endpoint 404s on the first call, and before this the whole sweep
        # died there — losing every earlier model's scores and writing no matrix at all.
        try:
            models_out[model] = _sweep_model(model, skill_system, base_system, holdout, log)
        except Exception as error:  # noqa: BLE001 - any provider failure is one unusable row
            models_out[model] = {"error": f"{type(error).__name__}: {error}"[:400]}
            log(f"[compat] {model:<34} UNAVAILABLE ({type(error).__name__}), skipped")
    if not any("error" not in row for row in models_out.values()):
        raise SystemExit(f"[compat] no model in COMPAT_MODELS could be reached for '{skill}'; "
                         f"nothing was measured.")

    summary = {"skill": skill, "tasks": len(holdout),
               "judge": os.environ.get("JUDGE_MODELS") or os.environ.get("JUDGE_MODEL", ""),
               "models": models_out, "usage": usage_ledger.report()}
    COMPAT_DIR.mkdir(parents=True, exist_ok=True)
    path = COMPAT_DIR / f"{skill}.json"
    path.write_text(json.dumps(summary, indent=2))
    log(f"[compat] matrix written to {path}")
    log(usage_ledger.format_report())
    return summary


if __name__ == "__main__":
    import sys

    from . import require_openrouter_key
    require_openrouter_key()
    run_compat(sys.argv[1] if len(sys.argv) > 1 else "tailwind")
