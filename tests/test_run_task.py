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


def test_serving_contract_requires_inline_deliverables():
    # the scaffold habit of writing code to its scratch FS and describing it must be countered in
    # BOTH serving contracts, symmetrically — production agent and A/B eval agent
    from agent.run import INSTRUCTIONS
    from optimize.ab import EVAL_INSTRUCTIONS
    for contract in (INSTRUCTIONS, EVAL_INSTRUCTIONS):
        assert "final answer must contain the complete deliverable" in contract
        assert "cannot" in contract and "workspace" in contract
