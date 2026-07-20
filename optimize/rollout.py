"""How a candidate skill is exercised and scored. One rollout renders the skill's components (its
routing `description`, its SKILL.md `body`, and any bundled `file:<path>` resources) into the shared
serving contract, answers one train task, and judges that answer against the task's rubric. The
judge's textual feedback is what the candidate search reads.

This module owns the rollout and the teacher (reflection) client only. The candidate search lives in
`optimize.skillopt_loop`; the held-out A/B that produces promotion evidence lives in `optimize.ab`.
"""
import os

from langchain_openai import ChatOpenAI

from . import agent_model

MODEL = agent_model()
# The teacher LM (the skill *author*): a stronger model than the serving agent, per the
# teacher/student split, where rollouts and judging stay on AGENT_MODEL (the model the skill will
# serve). GEPA_MODEL keeps its legacy name: it is still the reflection LM of the description pass's
# GEPA loop (optimize/routing.py), and it is the documented .env key.
GEPA_MODEL = os.environ.get("GEPA_MODEL", "z-ai/glm-5.2")

# Length penalty: the body re-enters context on every agent step, and a completeness-hungry judge
# tempts an author into bloating it. Penalize only *past* a generous target so normal skills aren't
# touched.
BODY_TARGET_CHARS = int(os.environ.get("BODY_TARGET_CHARS", "6000"))
LENGTH_PENALTY = float(os.environ.get("LENGTH_PENALTY", "0.10"))   # max score subtracted for a very long body


def length_penalty(body: str) -> float:
    over = max(0, len(body) - BODY_TARGET_CHARS) / BODY_TARGET_CHARS
    return min(LENGTH_PENALTY, LENGTH_PENALTY * over)              # 0 at/under target, capped above

# How rollouts execute a candidate skill. "direct" (default): one LLM call with the skill served
# under optimize.SERVE_TEMPLATE, the exact contract the quality A/B serves, so the candidate search
# can never optimize against different instructions than the A/B measures. "agent": the full
# deepagents scaffold (file tools and all) per rollout, which reproduces scaffold-driven failures
# the direct mode can't see (e.g. writing code to a scratch file instead of answering), at roughly
# A/B-call cost per rollout. Set GEPA_ROLLOUTS=agent to opt in.
# GEPA_ROLLOUTS is a legacy env name: it predates the removal of the GEPA body loop and now selects
# the rollout mode of the best-of-N candidate search. It is kept because it is a documented .env key.
GEPA_ROLLOUTS = os.environ.get("GEPA_ROLLOUTS", "direct")

from . import (SERVE_TEMPLATE, client_kwargs, is_openrouter, model_api_key,  # noqa: E402
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
    """Rolls a skill component dict out against train items ({"task", "rubric"}). `frozen`
    components (e.g. the routing description when only the body is searched) render into every
    rollout for serving fidelity but are never mutated."""

    def __init__(self, frozen: dict[str, str] | None = None):
        self._frozen = frozen or {}
        self._llm = None  # built lazily: agent-mode rollouts never need the direct client

    def _client(self):
        if self._llm is None:
            self._llm = ChatOpenAI(model=MODEL, temperature=0,
                                   **client_kwargs(model_base_url(), key=model_api_key(),
                                                   reasoning=True))
        return self._llm

    def serve(self, candidate: dict[str, str]) -> str:
        """The system prompt a rollout serves: frozen context plus this candidate, rendered under
        the one serving contract the held-out A/B also serves. Every caller goes through here so
        the search and the A/B can never disagree about what a skill looks like to a model."""
        return SERVE_TEMPLATE.format(body=assemble({**self._frozen, **candidate}))

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
        """One rollout through the full deepagents scaffold, file tools included, so the failure
        modes the scaffold invites (describe-instead-of-deliver) are visible to the search."""
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
    """Teacher callable: (str | messages) -> str. Our own litellm call instead of a framework's
    model-string plumbing, so the ZDR provider preference rides on every OpenRouter request, and a
    local OPENROUTER_BASE_URL (vLLM/Ollama) is honored through litellm's generic openai provider.
    Shared by the body pass's candidate authors and the description pass's GEPA reflection, whose
    LanguageModel protocol this signature satisfies."""
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
