"""Unit tests for agent.run.run_task message parsing (the agent LLM is faked)."""
import asyncio

from agent.run import behavior_events, run_task


class _Msg:
    def __init__(self, content=None, tool_calls=None, usage=None):
        self.content = content
        if tool_calls is not None:
            self.tool_calls = tool_calls
        if usage is not None:
            self.usage_metadata = usage


class _Agent:
    def __init__(self, messages):
        self._messages = messages

    async def ainvoke(self, _inp, config=None):
        return {"messages": self._messages}


def _run(messages):
    return asyncio.run(run_task(_Agent(messages), "some task"))


def test_run_task_sums_usage_and_extracts_loaded_skills():
    msgs = [
        _Msg(tool_calls=[{"name": "get_skill", "args": {"name": "pdf"}}], usage={"input_tokens": 10, "output_tokens": 5}),
        _Msg(tool_calls=[{"name": "suggest_skills", "args": {}}]),   # not a get_skill → not "loaded"
        _Msg(content="final answer", usage={"input_tokens": 20, "output_tokens": 8}),
    ]
    final, loaded, usage = _run(msgs)
    assert final == "final answer"
    assert loaded == ["pdf"]
    assert usage == {"input_tokens": 30, "output_tokens": 13}


def test_run_task_joins_list_content_blocks():
    final, _, _ = _run([_Msg(content=[{"text": "part 1"}, {"text": "part 2"}])])
    assert final == "part 1\npart 2"


def test_run_task_handles_no_skills_and_no_usage():
    final, loaded, usage = _run([_Msg(content="plain answer, no tools used")])
    assert final == "plain answer, no tools used"
    assert loaded == [] and usage == {"input_tokens": 0, "output_tokens": 0}


def test_run_task_empty_messages_returns_blank():
    final, loaded, usage = _run([])
    assert final == "" and loaded == [] and usage == {"input_tokens": 0, "output_tokens": 0}


def test_run_task_upgrades_loaded_name_with_revision_from_get_skill_result():
    msgs = [
        _Msg(tool_calls=[{"name": "get_skill", "args": {"name": "pdf"}}]),
        _Msg(content="# Skill: pdf@abc123\ndesc\n\nbody"),
        _Msg(content="done"),
    ]
    final, loaded, _ = _run(msgs)
    assert final == "done" and loaded == ["pdf@abc123"]
    # MCP adapters deliver tool results as content blocks, not plain strings
    msgs = [
        _Msg(tool_calls=[{"name": "get_skill", "args": {"name": "pdf"}}]),
        _Msg(content=[{"type": "text", "text": "# Skill: pdf@abc123\ndesc\n\nbody"}]),
        _Msg(content="done"),
    ]
    _, loaded, _ = _run(msgs)
    assert loaded == ["pdf@abc123"]


def test_run_task_extracts_loaded_skill_from_route_and_load_result():
    msgs = [
        _Msg(tool_calls=[{"name": "route_and_load", "args": {"task": "merge PDFs"}}]),
        _Msg(content='{"match":"pdf","revision":"abc","skill_body":"body"}'),
        _Msg(content="done"),
    ]
    final, loaded, _ = _run(msgs)
    assert final == "done" and loaded == ["pdf@abc"]


def test_behavior_events_capture_tool_order_and_hash_final_output():
    events = behavior_events([
        _Msg(tool_calls=[{"name": "route_and_load", "args": {"task": "merge PDFs"}},
                         {"name": "bash", "args": {"command": "python merge.py"}}]),
        _Msg(content="sensitive final answer"),
    ])
    assert [event["name"] for event in events[:-1]] == ["route_and_load", "bash"]
    assert events[-1]["type"] == "final" and len(events[-1]["sha256"]) == 64
    assert "sensitive final answer" not in str(events)


def test_build_agent_strong_flag_selects_model_and_endpoint(monkeypatch):
    # weak (default): MODEL on the serving endpoint; strong: strong_model() on the teacher
    # endpoint — the weak/strong split must never leak the wrong key or base_url across roles
    import agent.run as run_mod
    import deepagents
    import langchain_openai

    captured = {}

    class FakeChat:
        def __init__(self, model, temperature, base_url, api_key, extra_body):
            captured.update(model=model, base_url=base_url, api_key=api_key)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChat)
    monkeypatch.setattr(deepagents, "create_deep_agent",
                        lambda model, tools, system_prompt: {"prompt": system_prompt})
    monkeypatch.setenv("BASE_URL", "http://teacher:9000/v1")
    monkeypatch.setenv("API_KEY", "teacher-key")
    monkeypatch.setenv("MODEL_BASE_URL", "http://weak:8000/v1")
    monkeypatch.setenv("MODEL_API_KEY", "weak-key")
    monkeypatch.setenv("STRONG_MODEL", "strong/model")

    agent = run_mod.build_agent([])
    assert agent == {"prompt": run_mod.INSTRUCTIONS}
    assert captured == {"model": run_mod.MODEL, "base_url": "http://weak:8000/v1",
                        "api_key": "weak-key"}

    run_mod.build_agent([], strong=True)
    assert captured == {"model": "strong/model", "base_url": "http://teacher:9000/v1",
                        "api_key": "teacher-key"}


def test_strong_model_resolution(monkeypatch):
    # STRONG_MODEL wins; falls back to GEPA_MODEL (the offline teacher), then the literal default
    from agent.run import strong_model
    monkeypatch.delenv("STRONG_MODEL", raising=False)
    monkeypatch.delenv("GEPA_MODEL", raising=False)
    assert strong_model() == "z-ai/glm-5.2"
    monkeypatch.setenv("GEPA_MODEL", "teacher/model")
    assert strong_model() == "teacher/model"
    monkeypatch.setenv("STRONG_MODEL", "big/model")
    assert strong_model() == "big/model"


def test_langfuse_config_carries_tags_only_when_keys_are_set(monkeypatch):
    # the skill/revision trace tags ride on langfuse_tags metadata — and tracing must stay a
    # clean no-op when Langfuse isn't configured
    from agent.run import langfuse_config
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert langfuse_config(tags=["pdf"]) == {}
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    cfg = langfuse_config(tags=["demo", "pdf", "revision=pdf@r1"])
    assert cfg["metadata"]["langfuse_tags"] == ["demo", "pdf", "revision=pdf@r1"]
    assert cfg["callbacks"]


def test_serving_contract_requires_inline_deliverables():
    # the scaffold habit of writing code to its scratch FS and describing it must be countered in
    # BOTH serving contracts, symmetrically — production agent and A/B eval agent
    from agent.run import INSTRUCTIONS
    from optimize.ab import EVAL_INSTRUCTIONS
    for contract in (INSTRUCTIONS, EVAL_INSTRUCTIONS):
        assert "final answer must contain the complete deliverable" in contract
        assert "cannot" in contract and "workspace" in contract
