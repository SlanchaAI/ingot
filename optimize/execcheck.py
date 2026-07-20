"""Execution-based code validation, an *objective* signal that grounds the LLM judge so it can't be
talked into rating broken code highly (the judge reads code; it doesn't run it).

Always static first: extract python blocks and `ast.parse` them, catches "described code but
wrote none" and syntax errors. Execution is sandboxed by default and fails closed:

- `EXEC_SANDBOX=docker` (the default) runs the code in a throwaway locked-down container, no
  network, no mounts, dropped capabilities, nobody user, memory/pid limits (`SANDBOX_RUNTIME=runsc`
  upgrades to gVisor kernel isolation once installed). If docker is unreachable the check is
  inconclusive; there is never a silent fallback to bare execution.
- `EXEC_SANDBOX=1` (legacy) runs it as a bare subprocess with only a stripped env and a timeout ,
  same user, same filesystem, same network. Only use inside a disposable container you trust the
  judged code to roam.
- `EXEC_SANDBOX=off` disables execution entirely: static checks only, `check:` specs inconclusive.

Either way the failure is classified: a SyntaxError/NameError/ImportError means the code is
broken regardless of inputs, while a FileNotFoundError-style error is inconclusive (a missing test
fixture, not a code defect) and is NOT held against the answer."""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid

EXEC_MODE = os.environ.get("EXEC_SANDBOX", "docker")  # "docker" (default) | "1" (bare) | off
EXEC_SANDBOX = EXEC_MODE == "1"                       # the legacy bare-subprocess opt-in
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "ingot-optimize")
SANDBOX_RUNTIME = os.environ.get("SANDBOX_RUNTIME", "")     # e.g. runsc (gVisor), if installed
_CODE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)
_CODE_KEYWORDS = ("code", "script", "python", "function", "def ", "pypdf", "runnable")
# Formula-language tasks ("use the IF function", "instead of an error code") trip the keyword net
# without ever asking for Python; only these unambiguous signals override that exemption.
_FORMULA_HINTS = ("formula", "excel", "google sheets", "spreadsheet")
_STRONG_CODE = ("python", "script", "def ", "runnable", "openpyxl", "pandas", "pypdf", ".py")
# CLI tasks are the same trap from the other direction: a rubric quoting `docker ... python -m
# pytest` mentions "python", but the deliverable is a shell command, not a Python block.
_CLI_HINTS = ("docker", "command line", "command-line", "shell command", "terminal",
              "environment variable", "env var")
_PY_ASK = ("python script", "python code", "write python", "in python", ".py", "def ",
           "openpyxl", "pandas", "pypdf")
# runtime errors that mean "missing fixture / environment", not "the code is wrong"
_INCONCLUSIVE = ("FileNotFoundError", "PermissionError", "ConnectionError", "URLError", "OSError",
                 "ModuleNotFoundError")


def expects_code(task: str, rubric: str = "") -> bool:
    """Only code-shaped tasks get an execution check (a menu-planning skill shouldn't be 'no code =
    fail'). Spreadsheet-formula tasks are exempt unless they explicitly ask for Python: their rubrics
    say things like 'must use the IF function', which is a formula, not code, demanding a runnable
    Python block there zeroes correct answers (and teaches the optimizer to game the check)."""
    text = f"{task}\n{rubric}".lower()
    if any(k in text for k in _FORMULA_HINTS) and not any(k in text for k in _STRONG_CODE):
        return False
    if any(k in text for k in _CLI_HINTS) and not any(k in text for k in _PY_ASK):
        return False
    return any(k in text for k in _CODE_KEYWORDS + _STRONG_CODE)


def _python_blocks(answer: str) -> list[str]:
    # keep fenced blocks that look like code (call/assign/def/import/control-flow), not prose
    markers = ("import ", "def ", "=", "(", "return", "for ", "with ", "if ")
    return [b for b in _CODE.findall(answer) if any(k in b for k in markers)]


def _failure(stderr: str, error_status: str, note: str) -> dict:
    """Classify a nonzero-exit stderr: environment-shaped errors are inconclusive, the rest are
    genuine code defects."""
    last = (stderr.strip().splitlines() or ["nonzero exit"])[-1]
    if any(k in stderr for k in _INCONCLUSIVE):
        return {"status": "inconclusive", "detail": f"{last[:100]} ({note})"}
    return {"status": error_status, "detail": last[:120]}


def _sandbox(spec: dict, timeout: int) -> dict | None:
    """Run {fixture, code, assertion} through sandbox_driver in a throwaway locked-down container.
    Returns the driver's verdict dict, or None when the sandbox itself is unavailable or broke ,
    the caller reports inconclusive (fail closed); there is never a bare-subprocess fallback.
    The flags are the containment: no network, no mounts, read-only rootfs with tmpfs scratch,
    nobody user, dropped capabilities, memory/pid/cpu limits. SANDBOX_RUNTIME=runsc swaps in
    gVisor's userspace kernel for true syscall isolation once it's installed on the host."""
    name = f"ingot-sandbox-{uuid.uuid4().hex[:12]}"
    cmd = ["docker", "run", "--rm", "-i", "--name", name,
           "--network", "none", "--read-only",
           "--tmpfs", "/work:rw,size=64m,uid=65534,gid=65534",
           "--tmpfs", "/tmp:rw,size=16m,uid=65534,gid=65534",
           "--workdir", "/work", "--user", "65534:65534",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--memory", "512m", "--memory-swap", "512m", "--pids-limit", "128", "--cpus", "1",
           "--env", "PYTHONPATH=/app"]
    if SANDBOX_RUNTIME:
        cmd += ["--runtime", SANDBOX_RUNTIME]
    cmd += [SANDBOX_IMAGE, "python", "-m", "optimize.sandbox_driver"]
    try:
        run = subprocess.run(cmd, input=json.dumps({**spec, "timeout": timeout}),
                             capture_output=True, text=True, timeout=timeout * 3 + 30)
    except FileNotFoundError:
        return None                                   # no docker CLI in this environment
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True)
        return {"phase": "sandbox", "returncode": -1, "stderr": "", "timeout": True}
    if run.returncode != 0:
        return None                                   # docker infra failure (no socket, no image)
    try:
        return json.loads(run.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def check(answer: str) -> dict:
    """{status, detail}: no_code | syntax_error | code_error | inconclusive | ok."""
    blocks = _python_blocks(answer)
    if not blocks:
        return {"status": "no_code", "detail": "the answer contains no runnable python code block"}
    code = "\n\n".join(blocks)
    try:
        ast.parse(code)
    except SyntaxError as e:
        return {"status": "syntax_error", "detail": f"{e.msg} (line {e.lineno})"}
    if EXEC_MODE == "docker":
        verdict = _sandbox({"code": code}, timeout=10)
        if verdict is None:
            return {"status": "inconclusive", "detail": "sandbox unavailable (docker unreachable)"}
        if verdict.get("ok"):
            return {"status": "ok", "detail": "runs cleanly (sandboxed)"}
        if verdict.get("timeout"):
            return {"status": "inconclusive", "detail": "timed out (>10s)"}
        return _failure(verdict.get("stderr", ""), "code_error", "likely a missing input fixture")
    if not EXEC_SANDBOX:
        return {"status": "ok", "detail": "code parses (static check only, execution is off; "
                                          "EXEC_SANDBOX=docker enables the sandbox)"}
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        p = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=10,
                           env={"PATH": os.environ.get("PATH", "")})
    except subprocess.TimeoutExpired:
        return {"status": "inconclusive", "detail": "timed out (>10s)"}
    finally:
        os.unlink(path)
    if p.returncode == 0:
        return {"status": "ok", "detail": "runs cleanly"}
    return _failure(p.stderr, "code_error", "likely a missing input fixture")


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
    exiting nonzero = fail) against whatever artifacts the code produced. Executes only when
    EXEC_SANDBOX is set, `docker` (sandboxed, recommended) or `1` (bare subprocess, legacy; only
    inside a disposable container), and is inconclusive otherwise (fail closed). Statuses:
    no_code | syntax_error | fixture_error | exec_error | inconclusive | assert_failed | passed."""
    blocks = _python_blocks(answer)
    if not blocks:
        return {"status": "no_code", "detail": "the answer contains no runnable python code block"}
    code = "\n\n".join(blocks)
    try:
        ast.parse(code)
    except SyntaxError as e:
        return {"status": "syntax_error", "detail": f"{e.msg} (line {e.lineno})"}
    if EXEC_MODE == "docker":
        verdict = _sandbox({"fixture": fixture, "code": code, "assertion": assertion}, timeout)
        if verdict is None:
            return {"status": "inconclusive", "detail": "sandbox unavailable (docker unreachable)"}
        if verdict.get("ok"):
            return {"status": "passed", "detail": "code ran against the fixture and the assertion held"}
        phase = verdict.get("phase", "code")
        if verdict.get("timeout"):
            if phase == "fixture":
                return {"status": "fixture_error", "detail": "fixture timed out, inconclusive"}
            return {"status": "inconclusive", "detail": f"{phase} timed out (>{timeout}s)"}
        stderr = verdict.get("stderr", "")
        if phase == "fixture":
            last = (stderr.strip().splitlines() or ["fixture failed"])[-1]
            return {"status": "fixture_error", "detail": f"fixture failed: {last[:100]}, inconclusive"}
        if phase == "assertion":
            return _failure(stderr, "assert_failed", "assertion could not run")
        return _failure(stderr, "exec_error", "likely a missing dependency")
    if not EXEC_SANDBOX:
        return {"status": "inconclusive",
                "detail": "execution disabled, set EXEC_SANDBOX=docker (sandboxed, the default) "
                          "or EXEC_SANDBOX=1 (bare, legacy) to run check: specs"}
    with tempfile.TemporaryDirectory() as cwd:
        if fixture:
            try:
                setup = _run(fixture, cwd, timeout)
            except subprocess.TimeoutExpired:
                return {"status": "fixture_error", "detail": "fixture timed out, inconclusive"}
            if setup.returncode != 0:
                last = (setup.stderr.strip().splitlines() or ["fixture failed"])[-1]
                return {"status": "fixture_error", "detail": f"fixture failed: {last[:100]}, inconclusive"}
        try:
            run = _run(code, cwd, timeout)
        except subprocess.TimeoutExpired:
            return {"status": "inconclusive", "detail": f"timed out (>{timeout}s)"}
        if run.returncode != 0:
            return _failure(run.stderr, "exec_error", "likely a missing dependency")
        if assertion:
            try:
                verdict = _run(assertion, cwd, timeout)
            except subprocess.TimeoutExpired:
                return {"status": "inconclusive", "detail": "assertion timed out"}
            if verdict.returncode != 0:
                return _failure(verdict.stderr, "assert_failed", "assertion could not run")
    return {"status": "passed", "detail": "code ran against the fixture and the assertion held"}


def judge_note(answer: str, task: str, rubric: str = "", check_spec: dict | None = None,
               deliverable: str | None = None) -> str:
    """A one-line objective fact to hand the judge, or '' when there's nothing decisive to say.
    Tasks with a `check:` spec get the execution-grounded verdict; others get the static check.
    `deliverable` is the eval author's explicit override (task yaml `deliverable:`): any value other
    than "python"/"code" (e.g. "command", "css", "text") skips the static Python check entirely ,
    the keyword heuristic below has misfired on formulas, CLI commands, and CSS, so eval authors
    can simply declare what the answer is supposed to be."""
    if deliverable and deliverable.lower() not in ("python", "code"):
        return ""
    if check_spec:
        r = check_with_fixture(answer, check_spec.get("fixture", ""), check_spec.get("assert", ""),
                               timeout=int(check_spec.get("timeout", 15)))
        if r["status"] == "passed":
            return "OBJECTIVE EXECUTION CHECK, PASSED: the code ran against the task's fixture and the assertion held."
        if r["status"] in ("no_code", "syntax_error", "exec_error", "assert_failed"):
            return (f"OBJECTIVE EXECUTION CHECK, FAILED ({r['status']}): {r['detail']}. "
                    f"A correct answer must contain complete code that produces the required artifacts.")
        return ""  # fixture_error / inconclusive -> stay silent, the harness failed, not the answer
    if not expects_code(task, rubric):
        return ""
    r = check(answer)
    if r["status"] in ("no_code", "syntax_error", "code_error"):
        return f"OBJECTIVE CODE CHECK, FAILED: {r['detail']}. A correct answer must contain complete, valid code."
    if r["status"] == "ok":
        return f"OBJECTIVE CODE CHECK, {r['detail']}."
    return ""  # inconclusive -> stay silent, don't punish a missing fixture
