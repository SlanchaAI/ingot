"""Execution-based code validation — an *objective* signal that grounds the LLM judge so it can't be
talked into rating broken code highly (the judge reads code; it doesn't run it).

Static by default (safe): extract python blocks and `ast.parse` them — catches "described code but
wrote none" and syntax errors. Opt-in `EXEC_SANDBOX=1` additionally *runs* the code in a subprocess
with a timeout and classifies the failure: a SyntaxError/NameError/ImportError means the code is
broken regardless of inputs, while a FileNotFoundError-style error is inconclusive (a missing test
fixture, not a code defect) and is NOT held against the answer."""
import ast
import os
import re
import subprocess
import sys
import tempfile

EXEC_SANDBOX = os.environ.get("EXEC_SANDBOX", "") == "1"
_CODE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)
_CODE_KEYWORDS = ("code", "script", "python", "function", "def ", "pypdf", "runnable")
# runtime errors that mean "missing fixture / environment", not "the code is wrong"
_INCONCLUSIVE = ("FileNotFoundError", "PermissionError", "ConnectionError", "URLError", "OSError",
                 "ModuleNotFoundError")


def expects_code(task: str, rubric: str = "") -> bool:
    """Only code-shaped tasks get an execution check (a menu-planning skill shouldn't be 'no code = fail')."""
    text = f"{task}\n{rubric}".lower()
    return any(k in text for k in _CODE_KEYWORDS)


def _python_blocks(answer: str) -> list[str]:
    # keep fenced blocks that look like code (call/assign/def/import/control-flow), not prose
    markers = ("import ", "def ", "=", "(", "return", "for ", "with ", "if ")
    return [b for b in _CODE.findall(answer) if any(k in b for k in markers)]


def check(answer: str) -> dict:
    """{status, detail}: no_code | syntax_error | code_error | runtime_error (inconclusive) | ok."""
    blocks = _python_blocks(answer)
    if not blocks:
        return {"status": "no_code", "detail": "the answer contains no runnable python code block"}
    code = "\n\n".join(blocks)
    try:
        ast.parse(code)
    except SyntaxError as e:
        return {"status": "syntax_error", "detail": f"{e.msg} (line {e.lineno})"}
    if not EXEC_SANDBOX:
        return {"status": "ok", "detail": "code parses (static check; set EXEC_SANDBOX=1 to actually run it)"}
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        p = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=10,
                           env={"PATH": os.environ.get("PATH", "")})
    except subprocess.TimeoutExpired:
        return {"status": "runtime_error", "detail": "timed out (>10s) — inconclusive"}
    finally:
        os.unlink(path)
    if p.returncode == 0:
        return {"status": "ok", "detail": "runs cleanly"}
    last = (p.stderr.strip().splitlines() or ["nonzero exit"])[-1]
    if any(k in p.stderr for k in _INCONCLUSIVE):
        return {"status": "runtime_error", "detail": f"{last[:100]} (inconclusive — likely a missing input fixture)"}
    return {"status": "code_error", "detail": last[:120]}


def _run(code: str, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=cwd) as f:
        f.write(code)
        path = f.name
    try:
        return subprocess.run([sys.executable, path], capture_output=True, text=True,
                              timeout=timeout, cwd=cwd, env={"PATH": os.environ.get("PATH", "")})
    finally:
        os.unlink(path)


def check_with_fixture(answer: str, fixture: str = "", assertion: str = "", timeout: int = 15) -> dict:
    """Execution-grounded verdict for a task that ships a `check:` spec: seed a scratch directory
    with `fixture` (python), run the answer's code there, then run `assertion` (python; raising or
    exiting nonzero = fail) against whatever artifacts the code produced. A task author writing
    `check:` explicitly opts into execution — run this only inside the disposable optimize
    container. Statuses: no_code | syntax_error | fixture_error | exec_error | inconclusive |
    assert_failed | passed."""
    blocks = _python_blocks(answer)
    if not blocks:
        return {"status": "no_code", "detail": "the answer contains no runnable python code block"}
    code = "\n\n".join(blocks)
    try:
        ast.parse(code)
    except SyntaxError as e:
        return {"status": "syntax_error", "detail": f"{e.msg} (line {e.lineno})"}
    with tempfile.TemporaryDirectory() as cwd:
        if fixture:
            try:
                setup = _run(fixture, cwd, timeout)
            except subprocess.TimeoutExpired:
                return {"status": "fixture_error", "detail": "fixture timed out — inconclusive"}
            if setup.returncode != 0:
                last = (setup.stderr.strip().splitlines() or ["fixture failed"])[-1]
                return {"status": "fixture_error", "detail": f"fixture failed: {last[:100]} — inconclusive"}
        try:
            run = _run(code, cwd, timeout)
        except subprocess.TimeoutExpired:
            return {"status": "inconclusive", "detail": f"timed out (>{timeout}s)"}
        if run.returncode != 0:
            last = (run.stderr.strip().splitlines() or ["nonzero exit"])[-1]
            if any(k in run.stderr for k in _INCONCLUSIVE):
                return {"status": "inconclusive", "detail": f"{last[:100]} (likely a missing dependency)"}
            return {"status": "exec_error", "detail": last[:120]}
        if assertion:
            try:
                verdict = _run(assertion, cwd, timeout)
            except subprocess.TimeoutExpired:
                return {"status": "inconclusive", "detail": "assertion timed out"}
            if verdict.returncode != 0:
                last = (verdict.stderr.strip().splitlines() or ["assertion failed"])[-1]
                if any(k in verdict.stderr for k in _INCONCLUSIVE):
                    return {"status": "inconclusive", "detail": f"{last[:100]} (assertion could not run)"}
                return {"status": "assert_failed", "detail": last[:120]}
    return {"status": "passed", "detail": "code ran against the fixture and the assertion held"}


def judge_note(answer: str, task: str, rubric: str = "", check_spec: dict | None = None) -> str:
    """A one-line objective fact to hand the judge, or '' when there's nothing decisive to say.
    Tasks with a `check:` spec get the execution-grounded verdict; others get the static check."""
    if check_spec:
        r = check_with_fixture(answer, check_spec.get("fixture", ""), check_spec.get("assert", ""),
                               timeout=int(check_spec.get("timeout", 15)))
        if r["status"] == "passed":
            return "OBJECTIVE EXECUTION CHECK — PASSED: the code ran against the task's fixture and the assertion held."
        if r["status"] in ("no_code", "syntax_error", "exec_error", "assert_failed"):
            return (f"OBJECTIVE EXECUTION CHECK — FAILED ({r['status']}): {r['detail']}. "
                    f"A correct answer must contain complete code that produces the required artifacts.")
        return ""  # fixture_error / inconclusive -> stay silent, the harness failed, not the answer
    if not expects_code(task, rubric):
        return ""
    r = check(answer)
    if r["status"] in ("no_code", "syntax_error", "code_error"):
        return f"OBJECTIVE CODE CHECK — FAILED: {r['detail']}. A correct answer must contain complete, valid code."
    if r["status"] == "ok":
        return f"OBJECTIVE CODE CHECK — {r['detail']}."
    return ""  # inconclusive runtime error -> stay silent, don't punish a missing fixture
