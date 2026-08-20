"""Contract tests for the subscription-backed Agy judge process boundary."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ingot.optimize import agy_judge as A


FIXTURE = Path(__file__).parent / "fixtures" / "agy" / "judge-stream.jsonl"
CHECKLIST = [{"id": "probe", "criterion": "Return the requested token.", "weight": 1}]
TWO_ITEM_CHECKLIST = [
    *CHECKLIST,
    {"id": "second", "criterion": "Return the second requested token.", "weight": 1},
]
VALID_GRADE = {
    "items": {"probe": {"verdict": "pass", "note": ""}},
    "feedback": "ok",
}
VALID_USAGE = {
    "input_tokens": 1,
    "output_tokens": 1,
    "thinking_tokens": 0,
    "cache_read_tokens": 0,
    "total_tokens": 2,
}


def _terminal(**changes: object) -> dict:
    result = {
        "status": "SUCCESS",
        "structured_output": VALID_GRADE,
        "usage": VALID_USAGE,
    }
    result.update(changes)
    return {"event": "result", "result": result}


def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


def _runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    agy_bin = tmp_path / "agy"
    agy_bin.write_text("fixture executable")
    agy_bin.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("AGY_BIN", str(agy_bin))
    monkeypatch.setenv("AGY_JUDGE_WORKSPACE", str(workspace))
    return agy_bin, workspace


def test_captured_stream_supplies_the_fixed_judge_contract():
    grade, usage = A.parse_stream(FIXTURE.read_text(), CHECKLIST)
    assert A.AGY_MODEL == "gemini-3.6-flash-medium"
    assert A.AGY_IDENTITY == "agy/gemini-3.6-flash-medium"
    assert grade["items"]["probe"]["verdict"] == "pass"
    assert usage["total_tokens"] == 24067


def test_schema_requires_exact_checklist_items_and_feedback():
    assert A.judge_schema(CHECKLIST) == {
        "type": "object",
        "additionalProperties": False,
        "required": ["items", "feedback"],
        "properties": {
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["probe"],
                "properties": {
                    "probe": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["verdict", "note"],
                        "properties": {
                            "verdict": {"enum": ["pass", "partial", "fail"]},
                            "note": {"type": "string"},
                        },
                    }
                },
            },
            "feedback": {"type": "string"},
        },
    }


def test_child_environment_scrubs_provider_routing_and_disables_updates():
    parent = {
        "PATH": "/bin",
        "BASE_URL": "https://provider.invalid",
        "API_KEY": "secret-a",
        "MODEL_API_KEY": "secret-b",
        "OPENROUTER_API_KEY": "secret-c",
        "OPENAI_API_KEY": "secret-d",
        "ANTHROPIC_API_KEY": "secret-e",
        "GEMINI_API_KEY": "secret-f",
        "GOOGLE_API_KEY": "secret-g",
        "VERTEX_API_KEY": "secret-h",
        "AGY_CLI_DISABLE_AUTO_UPDATE": "false",
    }
    child = A.agy_process_env(parent)
    assert child == {"PATH": "/bin", "AGY_CLI_DISABLE_AUTO_UPDATE": "true"}
    assert "OPENROUTER_API_KEY" not in child


@pytest.mark.parametrize(
    "case,stdout",
    [
        ("zero terminal results", _jsonl({"event": "init"})),
        ("two terminal results", _jsonl(_terminal(), _terminal())),
        ("non-success status", _jsonl(_terminal(status="FAILED"))),
        (
            "missing structured output",
            _jsonl(_terminal(structured_output=None, response=json.dumps(VALID_GRADE))),
        ),
        (
            "missing checklist item",
            _jsonl(_terminal(structured_output={"items": {}, "feedback": "bad"})),
        ),
        (
            "extra checklist item",
            _jsonl(_terminal(structured_output={
                "items": {
                    **VALID_GRADE["items"],
                    "other": {"verdict": "fail", "note": "not requested"},
                },
                "feedback": "bad",
            })),
        ),
        (
            "unknown verdict",
            _jsonl(_terminal(structured_output={
                "items": {"probe": {"verdict": "excellent", "note": ""}},
                "feedback": "bad",
            })),
        ),
        (
            "invalid note",
            _jsonl(_terminal(structured_output={
                "items": {"probe": {"verdict": "pass", "note": ["not", "text"]}},
                "feedback": "bad",
            })),
        ),
        ("non-object event", "[]"),
        ("non-object result", _jsonl({"event": "result", "result": []})),
        (
            "missing usage",
            _jsonl({
                "event": "result",
                "result": {"status": "SUCCESS", "structured_output": VALID_GRADE},
            }),
        ),
        ("non-object usage", _jsonl(_terminal(usage=[]))),
        (
            "invalid feedback",
            _jsonl(_terminal(structured_output={
                "items": VALID_GRADE["items"],
                "feedback": ["not", "text"],
            })),
        ),
        (
            "extra grade key",
            _jsonl(_terminal(structured_output={**VALID_GRADE, "score": 1.0})),
        ),
        (
            "extra item key",
            _jsonl(_terminal(structured_output={
                "items": {"probe": {"verdict": "pass", "note": "", "score": 1.0}},
                "feedback": "bad",
            })),
        ),
        ("malformed jsonl", '{"event":"result"'),
    ],
)
def test_invalid_streams_raise_without_manufacturing_a_grade(case: str, stdout: str):
    with pytest.raises(A.AgyJudgeError, match="."):
        A.parse_stream(stdout, CHECKLIST)


def test_stream_parser_accepts_every_item_in_a_two_id_checklist():
    grade = {
        "items": {
            "probe": {"verdict": "pass", "note": ""},
            "second": {"verdict": "partial", "note": "one mismatch"},
        },
        "feedback": "Fix the second item.",
    }
    parsed, usage = A.parse_stream(
        _jsonl(_terminal(structured_output=grade, usage=VALID_USAGE)),
        TWO_ITEM_CHECKLIST,
    )
    assert parsed == grade
    assert usage == VALID_USAGE


def test_invoke_uses_only_the_explicit_sandboxed_process_boundary(monkeypatch, tmp_path):
    agy_bin, workspace = _runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-leak")
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=FIXTURE.read_text(), stderr="ignored")

    monkeypatch.setattr(A.subprocess, "run", fake_run)
    grade, usage = A.invoke("grade this", CHECKLIST, timeout=17.9)

    assert grade["items"]["probe"]["verdict"] == "pass"
    assert usage["total_tokens"] == 24067
    assert seen["argv"] == [
        str(agy_bin),
        "--model", A.AGY_MODEL,
        "--print", "grade this",
        "--output-format", "stream-json",
        "--json-schema", json.dumps(A.judge_schema(CHECKLIST)),
        "--sandbox",
        "--mode", "plan",
        "--disable-slash-commands",
        "--print-timeout", "17s",
    ]
    assert seen["cwd"] == workspace
    assert seen["text"] is True
    assert seen["capture_output"] is True
    assert seen["timeout"] == pytest.approx(27.9)
    assert seen["check"] is False
    assert "OPENROUTER_API_KEY" not in seen["env"]
    assert seen["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


@pytest.mark.parametrize("failure", ["timeout", "nonzero"])
def test_process_failures_raise_without_using_stdout_or_stderr_as_a_grade(
    failure, monkeypatch, tmp_path
):
    _runtime(monkeypatch, tmp_path)

    def fake_run(argv, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=FIXTURE.read_text())
        return subprocess.CompletedProcess(
            argv,
            9,
            stdout=FIXTURE.read_text(),
            stderr=json.dumps(VALID_GRADE),
        )

    monkeypatch.setattr(A.subprocess, "run", fake_run)
    with pytest.raises(A.AgyJudgeError, match="."):
        A.invoke("grade this", CHECKLIST)


def test_resource_exhaustion_is_rate_limited_and_retried(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path)
    calls = []
    sleeps = []
    clock = iter([0.0, 0.0, 3.2])

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                argv, 1,
                stdout=_jsonl({
                    "event": "result",
                    "result": {
                        "status": "ERROR",
                        "error": "Eligibility check failed: RESOURCE_EXHAUSTED (code 429)",
                    },
                }),
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout=FIXTURE.read_text(), stderr="")

    monkeypatch.setattr(A.subprocess, "run", fake_run)
    monkeypatch.setattr(A.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(A.time, "sleep", sleeps.append)
    A._next_launch_at = 0.0

    grade, _ = A.invoke("grade this", CHECKLIST)

    assert grade["items"]["probe"]["verdict"] == "pass"
    assert len(calls) == 2
    assert sleeps == [pytest.approx(3.2)]


def test_non_quota_process_failure_is_not_retried(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="not a quota result", stderr="")

    monkeypatch.setattr(A.subprocess, "run", fake_run)
    monkeypatch.setattr(A.time, "monotonic", lambda: 0.0)
    A._next_launch_at = 0.0

    with pytest.raises(A.AgyJudgeError, match="status 1"):
        A.invoke("grade this", CHECKLIST)
    assert len(calls) == 1


def test_preflight_records_version_and_requires_the_fixed_model(monkeypatch, tmp_path):
    agy_bin, workspace = _runtime(monkeypatch, tmp_path)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        stdout = "agy 1.1.11\n" if argv[-1] == "--version" else f"{A.AGY_MODEL}\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(A.subprocess, "run", fake_run)
    assert A.preflight() == {
        "identity": A.AGY_IDENTITY,
        "model": A.AGY_MODEL,
        "version": "agy 1.1.11",
        "billing_mode": "subscription",
    }
    assert [call[0] for call in calls] == [
        [str(agy_bin), "--version"],
        [str(agy_bin), "models"],
    ]
    assert all(call[1]["cwd"] == workspace for call in calls)
    assert all("OPENROUTER_API_KEY" not in call[1]["env"] for call in calls)


@pytest.mark.parametrize(
    "invalid_runtime",
    ["missing", "relative", "non_executable", "bad_workspace"],
)
def test_preflight_rejects_invalid_runtime_before_starting_a_process(
    invalid_runtime, monkeypatch, tmp_path
):
    agy_bin = tmp_path / "agy"
    agy_bin.write_text("fixture executable")
    agy_bin.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("AGY_BIN", str(agy_bin))
    monkeypatch.setenv("AGY_JUDGE_WORKSPACE", str(workspace))

    if invalid_runtime == "missing":
        monkeypatch.delenv("AGY_BIN")
    elif invalid_runtime == "relative":
        monkeypatch.setenv("AGY_BIN", "agy")
    elif invalid_runtime == "non_executable":
        agy_bin.chmod(0o600)
    else:
        bad_workspace = tmp_path / "workspace-file"
        bad_workspace.write_text("not a directory")
        monkeypatch.setenv("AGY_JUDGE_WORKSPACE", str(bad_workspace))

    calls = []
    monkeypatch.setattr(A.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(A.AgyJudgeError):
        A.preflight()
    assert calls == []


@pytest.mark.parametrize(
    "failed_command,failure,expected_calls",
    [
        ("version", "timeout", 1),
        ("version", "empty", 1),
        ("models", "nonzero", 2),
    ],
)
def test_preflight_stops_at_a_failed_command(
    failed_command, failure, expected_calls, monkeypatch, tmp_path
):
    _runtime(monkeypatch, tmp_path)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        command = "version" if argv[-1] == "--version" else "models"
        stdout = "agy 1.1.11\n" if command == "version" else f"{A.AGY_MODEL}\n"
        if command != failed_command:
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=stdout)
        if failure == "nonzero":
            return subprocess.CompletedProcess(argv, 7, stdout=stdout, stderr="grade-like text")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(A.subprocess, "run", fake_run)
    with pytest.raises(A.AgyJudgeError):
        A.preflight()
    assert len(calls) == expected_calls


def test_preflight_requires_the_exact_model_token(monkeypatch, tmp_path):
    _runtime(monkeypatch, tmp_path)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        stdout = "agy 1.1.11\n" if argv[-1] == "--version" else f"{A.AGY_MODEL}-preview\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(A.subprocess, "run", fake_run)
    with pytest.raises(A.AgyJudgeError):
        A.preflight()
    assert len(calls) == 2
