"""Unit tests for execution-based code validation (optimize.execcheck) — static path (no EXEC_SANDBOX)."""
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
