"""LLM judge: scores an answer 0..1 and — following the SkillForge paper's multi-dimensional Failure
Analyzer (Liu et al., "SkillForge", arXiv:2604.08618) — classifies each failure across fixed
dimensions so the optimizer gets *categorized* feedback, not one opaque score. The dimension labels
also drive success/failure mining (optimize/mine.py) and GEPA's reflective diagnosis.

Judges against a task `rubric` when given one; with no rubric it grades reference-free (used when
mining real traces). If a task supplies a `reference` answer, consistency-against-reference is added
to the prompt (the paper's Consistency-Rate signal, lower variance than a rubric alone)."""
import json
import os
import re
import time

from langchain_openai import ChatOpenAI

from . import usage as usage_ledger

# Reward-hacking guard: the judge must NOT be the same model as GEPA's reflection LM (GEPA_MODEL) —
# if the author and the grader share blind spots, GEPA learns to please the judge, not improve the
# skill. Default judge is a model distinct from both the reflection LM (GLM) and the student (Qwen).
# JUDGE_MODELS (comma-separated) runs an ensemble and averages — harder still to game.
MODELS = [m.strip() for m in os.environ.get(
    "JUDGE_MODELS", os.environ.get("JUDGE_MODEL", "google/gemini-2.5-flash")).split(",") if m.strip()]
from . import ZDR_PROVIDER, client_kwargs, teacher_base_url  # noqa: E402  (endpoint + ZDR policy)

if os.environ.get("GEPA_MODEL", "z-ai/glm-5.2") in MODELS:
    print(f"[judge] WARNING: judge model {MODELS} includes the GEPA reflection model — this invites "
          f"reward-hacking (author == grader). Set JUDGE_MODEL to a different model.", flush=True)

# Failure dimensions (the general-purpose analogue of the paper's Knowledge/Tool/Clarification/Style).
DIMENSIONS = ["correctness", "completeness", "instruction_following", "efficiency"]

_PROMPT = """You are grading an AI assistant's answer to a task.

TASK: {task}
{rubric_block}{reference_block}
ASSISTANT'S ANSWER:
{answer}
{code_block}
Score the answer from 0.0 to 1.0, and write one short paragraph of concrete, actionable feedback.
Treat any OBJECTIVE CODE CHECK above as ground truth — do not rate broken or absent code highly.
Then classify each failure dimension as "pass" or a short (<=12 word) note on what's wrong:
- correctness: is the core logic / API usage right?
- completeness: does it cover the whole request, including edge cases named above?
- instruction_following: did it do what was asked (e.g. output complete runnable code, not a description)?
- efficiency: is it concise, without wasted or padded output?

Respond with ONLY a JSON object:
{{"score": <float>, "feedback": "<paragraph>", "dimensions": {{"correctness": "...", "completeness": "...", "instruction_following": "...", "efficiency": "..."}}}}"""

_llms: dict[str, ChatOpenAI] = {}


def _get_llm(model: str):
    if model not in _llms:  # built once per model — reuses the HTTP pool across many judge calls
        _llms[model] = ChatOpenAI(model=model, temperature=0, **client_kwargs(teacher_base_url()))
    return _llms[model]


# OpenRouter phrasings that mean "your model/provider configuration can never work" — retrying
# only burns time, so explain and stop instead.
_PERMANENT = ("no allowed providers", "no providers are available", "not a valid model",
              "no endpoints found", "is not available")


def _config_error(exc: Exception) -> str | None:
    text = str(exc).lower()
    if any(marker in text for marker in _PERMANENT):
        pins = os.environ.get("OPENROUTER_PROVIDERS", "")
        hint = (f" You have OPENROUTER_PROVIDERS={pins} — the pinned provider may not serve this "
                f"model, or may not be ZDR-qualified for it; unset the pin or change the model."
                if pins else
                " No ZDR-qualified endpoint may exist for this model; try another model.")
        return f"OpenRouter cannot route this request: {exc}.{hint}"
    return None


def invoke_retry(llm, messages, tries: int = 3):
    """Retry transient provider failures (corrupted responses, 5xx) with a short backoff.
    Permanent configuration errors (model/provider mismatch) fail immediately with an explanation
    instead of retrying."""
    for i in range(tries):
        try:
            return llm.invoke(messages)
        except Exception as exc:
            explained = _config_error(exc)
            if explained:
                raise SystemExit(explained) from exc
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))


def _extract_json(text: str) -> dict:
    """First valid JSON object with a 'score' key — robust to prose/braces around the JSON."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "score" in obj:
            return obj
    return {}


def _judge_one(model: str, prompt: str) -> dict:
    msg = invoke_retry(_get_llm(model), prompt)
    usage_ledger.add("judge", getattr(msg, "usage_metadata", None))
    out = _extract_json(msg.content)
    try:
        dims = out.get("dimensions") or {}
        return {"score": max(0.0, min(1.0, float(out["score"]))), "feedback": str(out.get("feedback", "")),
                "dimensions": {d: str(dims.get(d, "pass")) for d in DIMENSIONS}}
    except (KeyError, TypeError, ValueError):
        return {"score": 0.0, "feedback": f"Judge output unparseable: {msg.content[:200]}",
                "dimensions": {d: "pass" for d in DIMENSIONS}}


def judge(task: str, rubric: str = "", answer: str = "", reference: str = "",
          check: dict | None = None) -> dict:
    """Return {score, feedback, dimensions}. With multiple JUDGE_MODELS this is an ensemble: score is
    the mean, and a dimension counts as failed if a majority of judges flag it (harder to game)."""
    rubric_block = f"GRADING RUBRIC: {rubric}\n" if rubric else ""
    reference_block = f"KNOWN-GOOD REFERENCE ANSWER (judge consistency against it): {reference}\n" if reference else ""
    from . import execcheck  # objective code-validity signal to ground the judge
    code_note = execcheck.judge_note(answer, task, rubric, check_spec=check)
    code_block = f"\n{code_note}\n" if code_note else ""
    prompt = _PROMPT.format(task=task, answer=answer, rubric_block=rubric_block,
                            reference_block=reference_block, code_block=code_block)
    results = [_judge_one(m, prompt) for m in MODELS]
    if len(results) == 1:
        return results[0]
    score = sum(r["score"] for r in results) / len(results)
    dims = {}
    for d in DIMENSIONS:
        notes = [r["dimensions"][d] for r in results if d in failed_dimensions(r["dimensions"])]
        dims[d] = notes[0] if len(notes) * 2 > len(results) else "pass"  # fail only on majority
    feedback = " | ".join(f"[{m.split('/')[-1]}] {r['feedback']}" for m, r in zip(MODELS, results))
    return {"score": score, "feedback": feedback, "dimensions": dims}


def failed_dimensions(dimensions: dict) -> list[str]:
    """Dimension names the judge did NOT mark as a clean pass."""
    return [d for d, v in dimensions.items() if str(v).strip().lower() not in ("pass", "ok", "", "n/a")]
