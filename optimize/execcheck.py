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


def judge_note(answer: str, task: str, rubric: str = "") -> str:
    """A one-line objective fact to hand the judge, or '' when there's nothing decisive to say."""
    if not expects_code(task, rubric):
        return ""
    r = check(answer)
    if r["status"] in ("no_code", "syntax_error", "code_error"):
        return f"OBJECTIVE CODE CHECK — FAILED: {r['detail']}. A correct answer must contain complete, valid code."
    if r["status"] == "ok":
        return f"OBJECTIVE CODE CHECK — {r['detail']}."
    return ""  # inconclusive runtime error -> stay silent, don't punish a missing fixture
