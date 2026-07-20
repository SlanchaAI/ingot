"""Cross-model skill compatibility, how well a skill's body transfers across serving models.

A skill body is tuned for one serving model (`AGENT_MODEL`); SkillOpt's own result is that good
skills transfer, but not always. For each model in `COMPAT_MODELS`, this runs the skill's held-out
tasks through the one serving contract twice, once with the skill body, once with an empty body
(the no-skill baseline), judges both with the FIXED judge, and reports per-model **lift**
(skill mean − baseline mean). Positive lift = the body helps that model; ~0 = the model already
knows this and the body is dead weight there.

Langfuse-free: it reuses the direct rollout + judge (the same path the inner loop uses), so it runs
in lite mode with the tracing stack down. Only the *serving* model varies, the judge stays fixed so
scores are comparable across models.

Usage:  python -m optimize.compat <skill>
Config: COMPAT_MODELS=qwen/qwen3-32b,openai/gpt-5.5,anthropic/claude-sonnet-...  (default: AGENT_MODEL)
"""
import json
import os
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from langchain_openai import ChatOpenAI

from mcp_server.registry import SKILLS_DIR, optimizable_components

from . import SERVE_TEMPLATE, agent_model, client_kwargs, model_api_key, model_base_url
from . import usage as usage_ledger
from .ab import load_tasks
from .judge import invoke_retry, judge
from .rollout import assemble

_MAX_WORKERS = 8
COMPAT_DIR = Path(__file__).resolve().parent.parent / "runs" / "compat"
# The no-skill baseline: the identical serving contract with no skill body, so `lift` isolates the
# body's contribution rather than the difference between two different prompts.
NO_SKILL_BODY = "(no skill loaded, answer the task from your own knowledge)"


def compat_models() -> list[str]:
    """Models to sweep: COMPAT_MODELS (comma-separated), else just the configured AGENT_MODEL."""
    models = [m.strip() for m in os.environ.get("COMPAT_MODELS", "").split(",") if m.strip()]
    return models or [agent_model()]


def _llm(model: str):
    # OpenRouter (default) selects the model by slug over one endpoint, with ZDR routing applied;
    # a local MODEL_BASE_URL serves a single model, so a multi-model sweep only makes sense on a
    # multi-model endpoint. reasoning is left at the provider default, some models reject the flag.
    return ChatOpenAI(model=model, temperature=0, **client_kwargs(model_base_url(), key=model_api_key()))


def _score(llm, system: str, task: dict) -> float:
    msg = invoke_retry(llm, [("system", system), ("user", task["task"])])
    usage_ledger.add("compat", getattr(msg, "usage_metadata", None))
    return judge(task["task"], task["rubric"], msg.content,
                 check=task.get("check"), deliverable=task.get("deliverable"))["score"]


def _run_arm(llm, system: str, tasks: list[dict]) -> list[float]:
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(tasks))) as pool:
        return list(pool.map(lambda t: _score(llm, system, t), tasks))


def run_compat(skill: str, log=print) -> dict:
    """Sweep COMPAT_MODELS over the skill's held-out tasks (skill vs no-skill) and write the matrix
    to runs/compat/<skill>.json. Returns the summary."""
    usage_ledger.reset()
    if not (SKILLS_DIR / skill / "SKILL.md").exists():
        raise SystemExit(f"No skill named '{skill}' in skills/.")
    _, holdout, _ = load_tasks(skill)
    if not holdout:
        raise SystemExit(f"'{skill}' has no held-out eval tasks to run.")
    skill_system = SERVE_TEMPLATE.format(body=assemble(optimizable_components(SKILLS_DIR / skill)))
    base_system = SERVE_TEMPLATE.format(body=NO_SKILL_BODY)
    models = compat_models()
    log(f"[compat] '{skill}': {len(holdout)} held-out tasks × {len(models)} model(s); "
        f"judge fixed, serving model varies")

    models_out = {}
    for model in models:
        llm = _llm(model)
        skill_scores = _run_arm(llm, skill_system, holdout)
        base_scores = _run_arm(llm, base_system, holdout)
        s_mean, b_mean = statistics.mean(skill_scores), statistics.mean(base_scores)
        models_out[model] = {"skill_mean": s_mean, "baseline_mean": b_mean, "lift": s_mean - b_mean,
                             "skill_scores": skill_scores, "baseline_scores": base_scores}
        verdict = "helps" if s_mean - b_mean > 0.05 else "no lift" if s_mean - b_mean >= -0.05 else "HURTS"
        log(f"[compat] {model:<34} skill {s_mean:.3f}  baseline {b_mean:.3f}  "
            f"lift {s_mean - b_mean:+.3f}  ({verdict})")

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
