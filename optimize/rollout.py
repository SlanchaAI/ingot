"""Shared rollout + reflection-LM infrastructure for the optimizer passes.

One rollout = a single direct LLM call whose system prompt embeds the assembled skill, judged
against the task's rubric; the judge's textual feedback drives the optimizer's reflection. Cheap and
isolates skill quality — the full-agent path is used for the final A/B. Used by the body-pass
optimizer (optimize.skillopt_loop) and the reflection LM shared with the routing/description pass
(optimize.routing)."""
import os

from langchain_openai import ChatOpenAI

from . import agent_model

MODEL = agent_model()
# The reflection LM (the skill *author*) — a stronger model than the serving agent, per the
# teacher/student split: rollouts + judging stay on AGENT_MODEL (the model the skill will serve).
GEPA_MODEL = os.environ.get("GEPA_MODEL", "z-ai/glm-5.2")

# Length penalty: the body re-enters context on every agent step, and a completeness-hungry judge
# tempts the optimizer to bloat it. Penalize only *past* a generous target so normal skills aren't touched.
BODY_TARGET_CHARS = int(os.environ.get("BODY_TARGET_CHARS", "6000"))
LENGTH_PENALTY = float(os.environ.get("LENGTH_PENALTY", "0.10"))   # max score subtracted for a very long body


def length_penalty(body: str) -> float:
    over = max(0, len(body) - BODY_TARGET_CHARS) / BODY_TARGET_CHARS
    return min(LENGTH_PENALTY, LENGTH_PENALTY * over)              # 0 at/under target, capped above

# How rollouts execute a candidate skill. "direct" (default): one LLM call with the skill served
# under optimize.SERVE_TEMPLATE — the exact contract the quality A/B serves, so the inner loop can
# never optimize against different instructions than the outer loop measures. "agent": the full
# deepagents scaffold (file tools and all) per rollout — reproduces scaffold-driven failures the
# direct mode can't see (e.g. writing code to a scratch file instead of answering), at roughly
# A/B-call cost per rollout. Set GEPA_ROLLOUTS=agent to opt in.
GEPA_ROLLOUTS = os.environ.get("GEPA_ROLLOUTS", "direct")

from . import (client_kwargs, is_openrouter, model_api_key,  # noqa: E402
               model_base_url, openrouter_extra_body, teacher_base_url)
from .judge import invoke_retry, judge  # noqa: E402
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
    """Runs one judged rollout of a candidate skill component dict. Batch items: {"task", "rubric"}.
    `frozen` components (e.g. the routing description when only the body is optimized) are
    rendered into every rollout for fidelity but are invisible to the optimizer's mutation."""

    def __init__(self, frozen: dict[str, str] | None = None):
        self._frozen = frozen or {}
        self._llm = None  # built lazily — agent-mode rollouts never need the direct client

    def _client(self):
        if self._llm is None:
            self._llm = ChatOpenAI(model=MODEL, temperature=0,
                                   **client_kwargs(model_base_url(), key=model_api_key(),
                                                   reasoning=True))
        return self._llm

    def _rollout(self, system, ex):
        if GEPA_ROLLOUTS == "agent":
            answer = self._agent_rollout(system, ex["task"])
        else:
            msg = invoke_retry(self._client(), [("system", system), ("user", ex["task"])])
            usage_ledger.add("rollout", getattr(msg, "usage_metadata", None))
            answer = msg.content
        j = judge(ex["task"], ex["rubric"], answer, reference=ex.get("reference", ""),
                  check=ex.get("check"), deliverable=ex.get("deliverable"))
        return answer, j["score"], {"task": ex["task"], "output": answer,
                                    "feedback": j["feedback"], "dimensions": j["dimensions"]}

    def _agent_rollout(self, system, task: str) -> str:
        """One rollout through the full deepagents scaffold — file tools included, so the failure
        modes the scaffold invites (describe-instead-of-deliver) are visible to the inner loop."""
        import asyncio

        from agent.run import build_agent, run_task
        agent = build_agent([], instructions=system)
        answer, _, usage = asyncio.run(run_task(agent, task))
        usage_ledger.add("rollout", usage)
        return answer


def _track_reflection(kwargs, response, start_time, end_time):  # litellm success callback
    u = getattr(response, "usage", None)
    if u:
        usage_ledger.add("reflection", {"input_tokens": getattr(u, "prompt_tokens", 0),
                                        "output_tokens": getattr(u, "completion_tokens", 0)})


def make_reflection_lm():
    """Reflection-LM callable: (str | messages) -> str. Our own litellm call so the ZDR provider
    preference rides on every OpenRouter reflection request — and a local OPENROUTER_BASE_URL
    (vLLM/Ollama) is honored via litellm's generic openai provider. Shared by the body (quality)
    and description (routing) passes."""
    import litellm
    litellm.success_callback = [_track_reflection]
    base = teacher_base_url()

    def reflection_lm(prompt) -> str:
        messages = prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
        if is_openrouter(base):
            response = litellm.completion(model=f"openrouter/{GEPA_MODEL}", messages=messages,
                                          extra_body=openrouter_extra_body())
        else:
            kwargs = client_kwargs(base)
            response = litellm.completion(model=f"openai/{GEPA_MODEL}", messages=messages,
                                          api_base=kwargs["base_url"], api_key=kwargs["api_key"])
        return response.choices[0].message.content

    return reflection_lm
