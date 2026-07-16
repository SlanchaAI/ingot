"""GEPA loop over a FULL skill: the candidate is the skill's components — its routing `description`,
its SKILL.md `body`, and any bundled `file:<path>` resources — and GEPA evolves all of them jointly.
One rollout = a single direct LLM call whose system prompt embeds the assembled skill, judged against
the task's rubric; the judge's textual feedback drives GEPA's reflection. Cheap and isolates skill
quality — the full-agent path is used for the final A/B."""
import os
from concurrent.futures import ThreadPoolExecutor

import gepa
from gepa import EvaluationBatch
from langchain_openai import ChatOpenAI

MODEL = os.environ.get("MODEL", "qwen/qwen3.6-27b")
# GEPA's reflection LM (the skill *author*) — a stronger model than the serving agent, per the
# teacher/student split: rollouts + judging stay on MODEL (the model the skill will serve).
GEPA_MODEL = os.environ.get("GEPA_MODEL", "z-ai/glm-5.2")
BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# Length penalty: the body re-enters context on every agent step, and a completeness-hungry judge
# tempts GEPA to bloat it. Penalize only *past* a generous target so normal skills aren't touched.
BODY_TARGET_CHARS = int(os.environ.get("BODY_TARGET_CHARS", "6000"))
LENGTH_PENALTY = float(os.environ.get("LENGTH_PENALTY", "0.10"))   # max score subtracted for a very long body


def length_penalty(body: str) -> float:
    over = max(0, len(body) - BODY_TARGET_CHARS) / BODY_TARGET_CHARS
    return min(LENGTH_PENALTY, LENGTH_PENALTY * over)              # 0 at/under target, capped above

ROLLOUT_SYSTEM = """You are an expert assistant. A skill (instructions for this kind of task, plus any
bundled reference files) is provided below — follow it to complete the user's task. Keep the answer concise.

{skill}"""

from .judge import ZDR_PROVIDER, invoke_retry, judge  # noqa: E402
from . import usage as usage_ledger  # noqa: E402


def assemble(candidate: dict[str, str]) -> str:
    """Render a component dict into the full skill text a model should follow: description, body,
    then each bundled file under its own header."""
    parts = [f"# Skill\n(when to use) {candidate.get('description', '')}", candidate["body"]]
    for key, content in candidate.items():
        if key.startswith("file:"):
            parts.append(f"# {key[len('file:'):]}\n{content}")
    return "\n\n".join(parts)


class SkillAdapter:
    """gepa.GEPAAdapter over the full-skill component dict {description, body, file:...}.
    Batch items: {"task", "rubric"}."""

    propose_new_texts = None  # gepa probes this optional hook; None -> use its default reflection

    def __init__(self):
        self._llm = ChatOpenAI(model=MODEL, base_url=BASE_URL, api_key=API_KEY, temperature=0,
                               extra_body=ZDR_PROVIDER)

    def _rollout(self, system, ex):
        msg = invoke_retry(self._llm, [("system", system), ("user", ex["task"])])
        usage_ledger.add("rollout", getattr(msg, "usage_metadata", None))
        answer = msg.content
        j = judge(ex["task"], ex["rubric"], answer, reference=ex.get("reference", ""))
        return answer, j["score"], {"task": ex["task"], "output": answer,
                                    "feedback": j["feedback"], "dimensions": j["dimensions"]}

    def evaluate(self, batch, candidate, capture_traces=False):
        system = ROLLOUT_SYSTEM.format(skill=assemble(candidate))
        # the hosted model is slow (~15-60s/call) — run the batch's rollout+judge pairs concurrently
        with ThreadPoolExecutor(max_workers=min(6, len(batch))) as pool:
            results = list(pool.map(lambda ex: self._rollout(system, ex), batch))
        outputs = [r[0] for r in results]
        # subtract a length penalty on the candidate body so GEPA can't win by bloating the skill
        penalty = length_penalty(candidate.get("body", ""))
        scores = [max(0.0, r[1] - penalty) for r in results]
        return EvaluationBatch(outputs=outputs, scores=scores,
                               trajectories=[r[2] for r in results] if capture_traces else None)

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        # Diagnose → minimal-edit (paper's Diagnostician): give reflection the *categorized* failure
        # dimensions per example, plus an aggregate of which dimensions fail most, and steer it toward
        # a targeted fix rather than a full rewrite.
        from .judge import failed_dimensions
        from collections import Counter
        trajs = eval_batch.trajectories or []
        agg = Counter(d for t in trajs for d in failed_dimensions(t.get("dimensions", {})))
        diagnosis = ("Most common failure dimensions across these tasks: "
                     + (", ".join(f"{d} ({n})" for d, n in agg.most_common()) or "none")
                     + ". Make the smallest targeted change that fixes the dominant dimension; "
                       "do not rewrite parts that already pass. Deletions need evidence: do not "
                       "remove guidance for operations these examples don't show failing — "
                       "tighten or restructure it instead. Component roles are fixed: `description` "
                       "is ONLY a routing trigger matched by embedding similarity against the user's "
                       "task — keep it a concise 'Use this skill when…' summary of trigger phrases, "
                       "never behavioral instructions. Every how-to-behave rule (e.g. 'always output "
                       "complete runnable code in the final answer') belongs in `body`.")
        records = []
        for t in trajs:
            failed = failed_dimensions(t.get("dimensions", {}))
            fb = t["feedback"]
            if failed:
                fb += "\nFailure dimensions: " + "; ".join(f"{d}: {t['dimensions'][d]}" for d in failed)
            records.append({"Inputs": t["task"], "Generated Outputs": t["output"],
                            "Feedback": fb, "Diagnosis": diagnosis})
        # same feedback informs every component GEPA chose to mutate this round
        return {comp: records for comp in components_to_update}


def _track_reflection(kwargs, response, start_time, end_time):  # litellm success callback
    u = getattr(response, "usage", None)
    if u:
        usage_ledger.add("reflection", {"input_tokens": getattr(u, "prompt_tokens", 0),
                                        "output_tokens": getattr(u, "completion_tokens", 0)})


def run_gepa(seed: dict[str, str], tasks: list[dict],
             max_metric_calls: int = 60) -> tuple[dict[str, str], float, float]:
    """Evolve the full skill (component dict) with GEPA.
    Returns (best_components, seed_score, best_score) on the task set."""
    import litellm
    litellm.success_callback = [_track_reflection]  # reflection goes through litellm below

    def reflection_lm(prompt) -> str:  # gepa's LanguageModel protocol: (str | messages) -> str
        # our own litellm call instead of gepa's model-string plumbing, so the ZDR provider
        # preference rides on every reflection request too
        messages = prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
        response = litellm.completion(model=f"openrouter/{GEPA_MODEL}", messages=messages,
                                      extra_body=ZDR_PROVIDER)
        return response.choices[0].message.content

    result = gepa.optimize(
        seed_candidate=seed,
        trainset=tasks,
        adapter=SkillAdapter(),
        reflection_lm=reflection_lm,
        max_metric_calls=max_metric_calls,
        display_progress_bar=True,
        raise_on_exception=False,  # a transient provider error shouldn't kill a 30-min run
    )
    seed_score = result.val_aggregate_scores[0]
    best_score = result.val_aggregate_scores[result.best_idx]
    return result.best_candidate, seed_score, best_score
