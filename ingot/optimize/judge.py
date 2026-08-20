"""LLM judge: scores an answer 0..1 and, following the SkillForge paper's multi-dimensional Failure
Analyzer (Liu et al., "SkillForge", arXiv:2604.08618), classifies each failure across fixed
dimensions so the search gets *categorized* feedback, not one opaque score. The dimension labels
also drive success/failure mining (optimize/mine.py) and the candidate search's diagnosis.

Judges against a task `rubric` when given one; with no rubric it grades reference-free (used when
mining real traces). If a task supplies a `reference` answer, consistency-against-reference is added
to the prompt (the paper's Consistency-Rate signal, lower variance than a rubric alone)."""
import json
import os
import re
import time

from langchain_openai import ChatOpenAI

from . import agy_judge
from . import configured_models
from . import usage as usage_ledger

# Reward-hacking guard: the judge must NOT be the same model as SKILLOPT_MODEL.
# If the author and the grader share blind spots, the search learns to please the judge instead of
# improving the skill. Default judge is a model distinct from both the reflection LM (GLM) and the
# student (Qwen).
# JUDGE_MODELS (comma-separated) runs an ensemble and averages, harder still to game. Repeating one
# ID is not an ensemble: it silently gives one grader multiple votes and pays for every duplicate.
MODELS = configured_models(
    "JUDGE_MODELS", os.environ.get("JUDGE_MODEL", "google/gemini-2.5-flash"))
from . import ZDR_PROVIDER, client_kwargs, skillopt_model, teacher_base_url  # noqa: E402

if skillopt_model() in MODELS:
    print(f"[judge] WARNING: judge model {MODELS} includes the teacher model, which invites "
          f"reward-hacking (author == grader). Set JUDGE_MODEL to a different model.", flush=True)

# Failure dimensions (the general-purpose analogue of the paper's Knowledge/Tool/Clarification/Style).
DIMENSIONS = ["correctness", "completeness", "instruction_following", "efficiency"]

# The score is the weighted mean of independent checks, never a number the model picks holistically.
# A single 0..1 judgement is one noisy measurement: in practice models emit it on a coarse ladder
# (0.90 / 0.95 / 1.00), so a mean can move by a whole rung because two tasks crossed a boundary, and
# re-running an unchanged arm moves it by that much. k independent checks average toward the truth
# roughly as sqrt(k). This is also what a graded checklist buys commercially -- Tessl grades ~38
# weighted items per run against our 4.
#
# A task may declare its own `checklist` (see optimize/draft.py); with none, every task is still
# graded on these four rather than on one holistic guess.
DEFAULT_CHECKLIST = [
    {"id": "correctness", "dimension": "correctness", "weight": 3,
     "criterion": "The core logic, API usage, and factual claims are right."},
    {"id": "completeness", "dimension": "completeness", "weight": 2,
     "criterion": "It covers the whole request, including any edge cases the task or rubric names."},
    {"id": "instruction_following", "dimension": "instruction_following", "weight": 2,
     "criterion": "It did what was asked, in the form asked for (e.g. complete runnable code, "
                  "not a description of code)."},
    {"id": "efficiency", "dimension": "efficiency", "weight": 1,
     "criterion": "It is concise, with no padded, repeated, or irrelevant output."},
]

# pass / partial / fail rather than a free float: a three-way verdict per item is a judgement a
# model makes reliably, and the resolution comes from having many of them, not from pretending a
# single one is precise to two decimals.
VERDICT_VALUES = {"pass": 1.0, "partial": 0.5, "fail": 0.0}

_PROMPT = """You are grading an AI assistant's answer to a task against a checklist.

TASK: {task}
{rubric_block}{reference_block}
ASSISTANT'S ANSWER:
{answer}
{code_block}
Grade EVERY checklist item independently. Judge each item only on what it asks about, and do not
let a good or bad impression of the answer overall carry across items. Treat any OBJECTIVE CODE
CHECK above as ground truth: do not pass a code item whose code is broken or absent.

CHECKLIST:
{checklist_block}
For each item give a verdict of "pass", "partial", or "fail", plus a note of at most 12 words.
The note is required when the verdict is not "pass" and must say what is wrong, not restate the
criterion. Then write one short paragraph of concrete, actionable feedback on the answer overall.

Respond with ONLY a JSON object:
{{"items": {{{item_shape}}}, "feedback": "<paragraph>"}}"""

_llms: dict[str, ChatOpenAI] = {}


def _get_llm(model: str):
    if model not in _llms:  # built once per model, reuses the HTTP pool across many judge calls
        _llms[model] = ChatOpenAI(model=model, temperature=0, **client_kwargs(teacher_base_url()))
    return _llms[model]


# OpenRouter phrasings that mean "your model/provider configuration can never work", retrying
# only burns time, so explain and stop instead.
_PERMANENT = ("no allowed providers", "no providers are available", "not a valid model",
              "no endpoints found", "is not available")


def _config_error(exc: Exception) -> str | None:
    text = str(exc).lower()
    if any(marker in text for marker in _PERMANENT):
        pins = os.environ.get("OPENROUTER_PROVIDERS", "")
        hint = (f" You have OPENROUTER_PROVIDERS={pins}, the pinned provider may not serve this "
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


def _extract_json(text: str, key: str = "score") -> dict:
    """First valid JSON object carrying `key`, robust to prose/braces around the JSON."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and key in obj:
            return obj
    return {}


def _verdict(raw) -> tuple[float, str]:
    """(value, note) from one item's grade. Accepts the strict {verdict, note} shape and the bare
    string a model sometimes emits instead. An unrecognized verdict reads as a fail with the raw
    text as its note: silently scoring it 1.0 would let a malformed grade inflate the result."""
    note = ""
    if isinstance(raw, dict):
        note = str(raw.get("note", "")).strip()
        raw = raw.get("verdict", "")
    word = str(raw).strip().lower()
    if word in VERDICT_VALUES:
        return VERDICT_VALUES[word], note
    return 0.0, note or f"ungraded ({str(raw)[:40]})"


def _judge_one(model: str, prompt: str, checklist: list[dict]) -> dict:
    if os.environ.get("JUDGE_BACKEND", "").strip().lower() == "agy":
        out, usage = agy_judge.invoke(prompt, checklist)
        usage_ledger.add("judge", usage, billing_mode="subscription")
        raw = json.dumps(out)
    else:
        msg = invoke_retry(_get_llm(model), prompt)
        usage_ledger.add("judge", getattr(msg, "usage_metadata", None))
        raw = msg.content
        out = _extract_json(raw, "items")
    items = out.get("items")
    if not isinstance(items, dict) or not items:
        return {"items": {}, "feedback": f"Judge output unparseable: {raw[:200]}",
                "unparseable": True}
    graded = {}
    for item in checklist:
        # a missing item is not a pass; the judge was asked for it and did not answer
        value, note = _verdict(items.get(item["id"], "")) if item["id"] in items else (0.0, "not graded")
        graded[item["id"]] = {"value": value, "note": note}
    return {"items": graded, "feedback": str(out.get("feedback", "")), "unparseable": False}


def _weighted(graded: dict, checklist: list[dict]) -> float:
    total = sum(float(i.get("weight", 1)) for i in checklist) or 1.0
    return sum(float(i.get("weight", 1)) * graded[i["id"]]["value"]
               for i in checklist if i["id"] in graded) / total


def judge(task: str, rubric: str = "", answer: str = "", reference: str = "",
          check: dict | None = None, deliverable: str | None = None,
          checklist: list[dict] | None = None) -> dict:
    """Return {score, feedback, dimensions, checklist}.

    `score` is the weighted mean of the checklist verdicts, not a number the judge chose. `checklist`
    carries the per-item verdicts so a reviewer can see which checks moved rather than only that the
    mean did. `dimensions` keeps its old prose shape for the candidate search and trace mining.

    With multiple JUDGE_MODELS this is an ensemble: each item's value is averaged across judges
    before weighting, which is smoother than averaging whole-answer scores, and a dimension counts
    as failed only when a majority of judges failed an item mapped to it.

    `deliverable` (task yaml) declares the expected answer kind; non-code values skip the static
    Python check, see execcheck.judge_note."""
    checklist = [i for i in (checklist or DEFAULT_CHECKLIST) if i.get("id") and i.get("criterion")]
    if not checklist:
        checklist = DEFAULT_CHECKLIST
    rubric_block = f"GRADING RUBRIC: {rubric}\n" if rubric else ""
    reference_block = f"KNOWN-GOOD REFERENCE ANSWER (judge consistency against it): {reference}\n" if reference else ""
    from . import execcheck  # objective code-validity signal to ground the judge
    code_note = execcheck.judge_note(answer, task, rubric, check_spec=check, deliverable=deliverable)
    code_block = f"\n{code_note}\n" if code_note else ""
    checklist_block = "\n".join(f"- {i['id']} (weight {i.get('weight', 1)}): {i['criterion']}"
                                for i in checklist)
    item_shape = ", ".join(f'"{i["id"]}": {{"verdict": "pass|partial|fail", "note": "..."}}'
                           for i in checklist)
    prompt = _PROMPT.format(task=task, answer=answer, rubric_block=rubric_block,
                            reference_block=reference_block, code_block=code_block,
                            checklist_block=checklist_block, item_shape=item_shape)
    models = ([agy_judge.AGY_IDENTITY]
              if os.environ.get("JUDGE_BACKEND", "").strip().lower() == "agy" else MODELS)
    results = [_judge_one(m, prompt, checklist) for m in models]

    usable = [r for r in results if not r["unparseable"]]
    if not usable:  # a parse failure is not a skill failure, but it must not read as a clean pass
        return {"score": 0.0, "feedback": results[0]["feedback"],
                "dimensions": {d: "pass" for d in DIMENSIONS},
                "checklist": {i["id"]: {"value": 0.0, "note": "judge output unparseable"}
                              for i in checklist}}

    merged = {i["id"]: {"value": sum(r["items"][i["id"]]["value"] for r in usable) / len(usable),
                        "note": next((r["items"][i["id"]]["note"] for r in usable
                                      if r["items"][i["id"]]["value"] < 1.0
                                      and r["items"][i["id"]]["note"]), "")}
              for i in checklist}
    score = _weighted(merged, checklist)

    # A dimension fails when the items mapped to it did not clean-pass across a majority of judges.
    dims = {d: "pass" for d in DIMENSIONS}
    for item in checklist:
        d = item.get("dimension")
        entry = merged[item["id"]]
        if d in dims and entry["value"] < 0.5 and dims[d] == "pass":
            dims[d] = entry["note"] or f"failed check '{item['id']}'"
    feedback = " | ".join(f"[{m.split('/')[-1]}] {r['feedback']}"
                          for m, r in zip(models, results) if r["feedback"]) \
        if len(results) > 1 else usable[0]["feedback"]
    return {"score": score, "feedback": feedback, "dimensions": dims, "checklist": merged}


def failed_dimensions(dimensions: dict) -> list[str]:
    """Dimension names the judge did NOT mark as a clean pass."""
    return [d for d, v in dimensions.items() if str(v).strip().lower() not in ("pass", "ok", "", "n/a")]
