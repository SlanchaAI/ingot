"""Unit tests for execution-based code validation (optimize.execcheck) — static path (no EXEC_SANDBOX)."""
import os
import subprocess
import sys

from optimize import execcheck as E


def test_expects_code_gates_on_task_shape():
    assert E.expects_code("Write a Python script to merge PDFs", "")
    assert E.expects_code("do it", "must contain runnable code")
    assert not E.expects_code("Plan a low-FODMAP dinner menu", "cover food selection and portions")


def test_check_flags_missing_code():
    assert E.check("Here's how it works: first you open the file, then...")["status"] == "no_code"


def test_check_flags_syntax_error():
    r = E.check("```python\ndef f(:\n    return 1\n```")
    assert r["status"] == "syntax_error"


def test_check_passes_valid_code_statically():
    r = E.check("```python\nfrom pypdf import PdfWriter\nw = PdfWriter()\nprint('ok')\n```")
    assert r["status"] == "ok"


def test_judge_note_only_fires_for_code_tasks():
    # judge_note(answer, task, rubric): a broken-code answer to a code task -> objective FAIL note
    note = E.judge_note("```python\ndef f(:\n```", "Write Python to merge PDFs", "runnable code")
    assert "FAILED" in note
    # the same missing code on a non-code task -> no note (a menu answer isn't "broken code")
    assert E.judge_note("Monday: salad. Tuesday: soup.", "Plan a dinner menu", "food choices") == ""


def test_judge_note_reports_pass_for_good_code():
    note = E.judge_note("```python\nimport os\nprint(os.getcwd())\n```", "Write a Python script", "code")
    assert "OBJECTIVE CODE CHECK" in note and "FAILED" not in note


# --- opt-in sandbox: actually runs the code (trivial, safe snippets) ---------------------------

def test_sandbox_runs_valid_code(monkeypatch):
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)
    assert E.check("```python\nx = sum(range(5))\nprint(x)\n```")["status"] == "ok"


def test_sandbox_catches_runtime_code_error(monkeypatch):
    # a NameError is a genuine code defect regardless of inputs -> code_error (held against the answer)
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)
    assert E.check("```python\nresult = undefined_name_xyz + 1\n```")["status"] == "code_error"


def test_sandbox_treats_missing_fixture_as_inconclusive(monkeypatch):
    # a missing input file is the environment's fault, not the code's -> inconclusive, not punished
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)
    assert E.check("```python\nopen('/no/such/fixture_zzz.pdf', 'rb')\n```")["status"] == "runtime_error"


def test_exec_sandbox_env_flag_is_the_real_optin():
    # the tests above patch the module flag; this one exercises the actual opt-in surface —
    # EXEC_SANDBOX=1 in the environment must flip the flag at import (and only "1" counts)
    snippet = ("from optimize.execcheck import EXEC_SANDBOX, check; "
               "print(EXEC_SANDBOX, check('```python\\nprint(1)\\n```')['detail'])")
    on = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True,
                        env={**os.environ, "EXEC_SANDBOX": "1"})
    assert on.stdout.startswith("True") and "runs cleanly" in on.stdout
    off = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True,
                         env={k: v for k, v in os.environ.items() if k != "EXEC_SANDBOX"})
    assert off.stdout.startswith("False") and "static check" in off.stdout


def test_sandbox_does_not_leak_host_env(monkeypatch):
    # the subprocess gets only PATH — secrets in the harness env must be invisible to judged code
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "hunter2")
    probe = ("```python\nimport os, sys\n"
             "sys.exit(13 if os.environ.get('SUPER_SECRET_TOKEN') else 0)\n```")
    assert E.check(probe)["status"] == "ok"          # exit 0: the secret was not visible


def test_sandbox_timeout_is_inconclusive(monkeypatch):
    # faked runner so the test doesn't wait out the real 10s ceiling
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="python", timeout=10)

    monkeypatch.setattr(E.subprocess, "run", hang)
    r = E.check("```python\nprint(1)\n```")
    assert r["status"] == "runtime_error" and "timed out" in r["detail"]


ANSWER_OK = """Here you go:
```python
text = open("input.txt").read()
open("output.txt", "w").write(text.upper())
```"""

CHECK = {"fixture": 'open("input.txt", "w").write("hello")',
         "assert": 'assert open("output.txt").read() == "HELLO", "wrong content"'}


def test_fixture_check_passes_end_to_end():
    r = E.check_with_fixture(ANSWER_OK, CHECK["fixture"], CHECK["assert"])
    assert r["status"] == "passed"


def test_fixture_check_fails_on_wrong_artifact():
    wrong = ANSWER_OK.replace(".upper()", ".lower()")
    r = E.check_with_fixture(wrong, CHECK["fixture"], CHECK["assert"])
    assert r["status"] == "assert_failed" and "wrong content" in r["detail"]


def test_fixture_check_exec_error_and_no_code():
    boom = 'Broken:\n```python\nraise ValueError("kaput")\n```'
    assert E.check_with_fixture(boom, "", "print(1)")["status"] == "exec_error"
    assert E.check_with_fixture("no code here", "", "")["status"] == "no_code"


def test_fixture_check_missing_dependency_is_inconclusive():
    needs_lib = "```python\nimport nonexistent_pdf_lib\n```"
    r = E.check_with_fixture(needs_lib, "", "print(1)")
    assert r["status"] == "inconclusive"


def test_broken_fixture_is_the_harness_fault():
    r = E.check_with_fixture(ANSWER_OK, 'raise RuntimeError("bad fixture")', "print(1)")
    assert r["status"] == "fixture_error"


def test_fixture_check_code_timeout_is_inconclusive():
    r = E.check_with_fixture("```python\nimport time\ntime.sleep(5)\n```", "", "print(1)", timeout=1)
    assert r["status"] == "inconclusive" and "timed out" in r["detail"]


def test_fixture_check_assertion_harness_failure_is_inconclusive():
    # the answer's code is fine; the *assertion* hits a missing file -> harness fault, not the answer's
    r = E.check_with_fixture(ANSWER_OK, CHECK["fixture"], 'open("/no/such/dir/expected.json")')
    assert r["status"] == "inconclusive" and "assertion could not run" in r["detail"]


def test_prose_fenced_block_is_not_code():
    assert E.check("```\njust prose in a fence, nothing runnable\n```")["status"] == "no_code"


def test_judge_note_threads_check_timeout_and_stays_silent_on_it():
    # a `timeout:` key in the task's check spec must reach the runner; the timeout is the
    # harness's ceiling, so the note stays silent rather than punishing the answer
    note = E.judge_note("```python\nimport time\ntime.sleep(5)\n```", "task",
                        check_spec={"fixture": "", "assert": "", "timeout": 1})
    assert note == ""


def test_judge_note_execution_verdicts():
    note = E.judge_note(ANSWER_OK, "task", check_spec=CHECK)
    assert "EXECUTION CHECK — PASSED" in note
    wrong = ANSWER_OK.replace(".upper()", ".lower()")
    note = E.judge_note(wrong, "task", check_spec=CHECK)
    assert "EXECUTION CHECK — FAILED (assert_failed)" in note
    # harness failures stay silent — never punish the answer for our broken fixture
    assert E.judge_note(ANSWER_OK, "task",
                                check_spec={"fixture": "raise RuntimeError()", "assert": ""}) == ""


def test_judge_threads_check_spec_into_the_prompt(monkeypatch):
    from optimize import judge as judge_mod
    seen = {}
    monkeypatch.setattr(judge_mod, "MODELS", ["m"])
    def capture(model, prompt):
        seen["prompt"] = prompt
        return {"score": 1.0, "feedback": "f", "dimensions": {d: "pass" for d in judge_mod.DIMENSIONS}}
    monkeypatch.setattr(judge_mod, "_judge_one", capture)
    judge_mod.judge("t", "r", ANSWER_OK, check=CHECK)
    assert "EXECUTION CHECK — PASSED" in seen["prompt"]
    judge_mod.judge("t", "r", ANSWER_OK)                    # no check -> static path only
    assert "EXECUTION CHECK" not in seen["prompt"]
