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


def test_check_passes_valid_code_statically(monkeypatch):
    monkeypatch.setattr(E, "EXEC_MODE", "off")
    r = E.check("```python\nfrom pypdf import PdfWriter\nw = PdfWriter()\nprint('ok')\n```")
    assert r["status"] == "ok" and "static check" in r["detail"]


def test_judge_note_only_fires_for_code_tasks():
    # judge_note(answer, task, rubric): a broken-code answer to a code task -> objective FAIL note
    note = E.judge_note("```python\ndef f(:\n```", "Write Python to merge PDFs", "runnable code")
    assert "FAILED" in note
    # the same missing code on a non-code task -> no note (a menu answer isn't "broken code")
    assert E.judge_note("Monday: salad. Tuesday: soup.", "Plan a dinner menu", "food choices") == ""


def test_judge_note_reports_pass_for_good_code(monkeypatch):
    monkeypatch.setattr(E, "EXEC_MODE", "off")
    note = E.judge_note("```python\nimport os\nprint(os.getcwd())\n```", "Write a Python script", "code")
    assert "OBJECTIVE CODE CHECK" in note and "FAILED" not in note


# --- opt-in sandbox: actually runs the code (trivial, safe snippets) ---------------------------

def test_sandbox_runs_valid_code(monkeypatch):
    monkeypatch.setattr(E, "EXEC_MODE", "1")
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)
    assert E.check("```python\nx = sum(range(5))\nprint(x)\n```")["status"] == "ok"


def test_sandbox_catches_runtime_code_error(monkeypatch):
    # a NameError is a genuine code defect regardless of inputs -> code_error (held against the answer)
    monkeypatch.setattr(E, "EXEC_MODE", "1")
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)
    assert E.check("```python\nresult = undefined_name_xyz + 1\n```")["status"] == "code_error"


def test_sandbox_treats_missing_fixture_as_inconclusive(monkeypatch):
    # a missing input file is the environment's fault, not the code's -> inconclusive, not punished
    monkeypatch.setattr(E, "EXEC_MODE", "1")
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)
    assert E.check("```python\nopen('/no/such/fixture_zzz.pdf', 'rb')\n```")["status"] == "runtime_error"


def test_exec_sandbox_env_modes():
    # fresh-import subprocesses exercise the real env surface: the sandbox is the DEFAULT,
    # "1" is the legacy bare opt-in, anything else turns execution off entirely
    base = {k: v for k, v in os.environ.items() if k != "EXEC_SANDBOX"}
    probe = "from optimize.execcheck import EXEC_MODE, EXEC_SANDBOX, check; "
    default = subprocess.run([sys.executable, "-c", probe + "print(EXEC_MODE)"],
                             capture_output=True, text=True, env=base)
    assert default.stdout.strip() == "docker"
    bare = subprocess.run(
        [sys.executable, "-c", probe + "print(EXEC_SANDBOX, check('```python\\nprint(1)\\n```')['detail'])"],
        capture_output=True, text=True, env={**base, "EXEC_SANDBOX": "1"})
    assert bare.stdout.startswith("True") and "runs cleanly" in bare.stdout
    off = subprocess.run(
        [sys.executable, "-c", probe + "print(check('```python\\nprint(1)\\n```')['detail'])"],
        capture_output=True, text=True, env={**base, "EXEC_SANDBOX": "off"})
    assert "static check" in off.stdout


def test_sandbox_does_not_leak_host_env(monkeypatch):
    # the subprocess gets only PATH — secrets in the harness env must be invisible to judged code
    monkeypatch.setattr(E, "EXEC_MODE", "1")
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "hunter2")
    probe = ("```python\nimport os, sys\n"
             "sys.exit(13 if os.environ.get('SUPER_SECRET_TOKEN') else 0)\n```")
    assert E.check(probe)["status"] == "ok"          # exit 0: the secret was not visible


def test_sandbox_timeout_is_inconclusive(monkeypatch):
    # faked runner so the test doesn't wait out the real 10s ceiling
    monkeypatch.setattr(E, "EXEC_MODE", "1")
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


def test_fixture_check_passes_end_to_end(monkeypatch):
    monkeypatch.setattr(E, "EXEC_MODE", "1")
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)  # legacy bare path
    r = E.check_with_fixture(ANSWER_OK, CHECK["fixture"], CHECK["assert"])
    assert r["status"] == "passed"


def test_fixture_check_fails_on_wrong_artifact(monkeypatch):
    monkeypatch.setattr(E, "EXEC_MODE", "1")
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)  # legacy bare path
    wrong = ANSWER_OK.replace(".upper()", ".lower()")
    r = E.check_with_fixture(wrong, CHECK["fixture"], CHECK["assert"])
    assert r["status"] == "assert_failed" and "wrong content" in r["detail"]


def test_fixture_check_exec_error_and_no_code(monkeypatch):
    monkeypatch.setattr(E, "EXEC_MODE", "1")
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)  # legacy bare path
    boom = 'Broken:\n```python\nraise ValueError("kaput")\n```'
    assert E.check_with_fixture(boom, "", "print(1)")["status"] == "exec_error"
    assert E.check_with_fixture("no code here", "", "")["status"] == "no_code"


def test_fixture_check_missing_dependency_is_inconclusive(monkeypatch):
    monkeypatch.setattr(E, "EXEC_MODE", "1")
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)  # legacy bare path
    needs_lib = "```python\nimport nonexistent_pdf_lib\n```"
    r = E.check_with_fixture(needs_lib, "", "print(1)")
    assert r["status"] == "inconclusive"


def test_broken_fixture_is_the_harness_fault(monkeypatch):
    monkeypatch.setattr(E, "EXEC_MODE", "1")
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)  # legacy bare path
    r = E.check_with_fixture(ANSWER_OK, 'raise RuntimeError("bad fixture")', "print(1)")
    assert r["status"] == "fixture_error"


def test_fixture_check_code_timeout_is_inconclusive(monkeypatch):
    monkeypatch.setattr(E, "EXEC_MODE", "1")
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)  # legacy bare path
    r = E.check_with_fixture("```python\nimport time\ntime.sleep(5)\n```", "", "print(1)", timeout=1)
    assert r["status"] == "inconclusive" and "timed out" in r["detail"]


def test_fixture_check_assertion_harness_failure_is_inconclusive(monkeypatch):
    monkeypatch.setattr(E, "EXEC_MODE", "1")
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)  # legacy bare path
    # the answer's code is fine; the *assertion* hits a missing file -> harness fault, not the answer's
    r = E.check_with_fixture(ANSWER_OK, CHECK["fixture"], 'open("/no/such/dir/expected.json")')
    assert r["status"] == "inconclusive" and "assertion could not run" in r["detail"]


def test_prose_fenced_block_is_not_code():
    assert E.check("```\njust prose in a fence, nothing runnable\n```")["status"] == "no_code"


def test_judge_note_threads_check_timeout_and_stays_silent_on_it(monkeypatch):
    monkeypatch.setattr(E, "EXEC_MODE", "1")
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)  # legacy bare path
    # a `timeout:` key in the task's check spec must reach the runner; the timeout is the
    # harness's ceiling, so the note stays silent rather than punishing the answer
    note = E.judge_note("```python\nimport time\ntime.sleep(5)\n```", "task",
                        check_spec={"fixture": "", "assert": "", "timeout": 1})
    assert note == ""


def test_judge_note_execution_verdicts(monkeypatch):
    monkeypatch.setattr(E, "EXEC_MODE", "1")
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)  # legacy bare path
    note = E.judge_note(ANSWER_OK, "task", check_spec=CHECK)
    assert "EXECUTION CHECK — PASSED" in note
    wrong = ANSWER_OK.replace(".upper()", ".lower()")
    note = E.judge_note(wrong, "task", check_spec=CHECK)
    assert "EXECUTION CHECK — FAILED (assert_failed)" in note
    # harness failures stay silent — never punish the answer for our broken fixture
    assert E.judge_note(ANSWER_OK, "task",
                                check_spec={"fixture": "raise RuntimeError()", "assert": ""}) == ""


def test_judge_threads_check_spec_into_the_prompt(monkeypatch):
    monkeypatch.setattr(E, "EXEC_MODE", "1")
    monkeypatch.setattr(E, "EXEC_SANDBOX", True)  # legacy bare path
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


# --- docker sandbox mode (EXEC_SANDBOX=docker): the docker CLI is faked; the driver is real -----

def _fake_docker(monkeypatch, stdout='{"ok": true}', returncode=0, raise_exc=None):
    calls = {}

    def run(cmd, **kwargs):
        if raise_exc:
            raise raise_exc
        calls["cmd"] = cmd
        calls["input"] = kwargs.get("input", "")
        return subprocess.CompletedProcess(args=cmd, returncode=returncode,
                                           stdout=stdout, stderr="")

    monkeypatch.setattr(E, "EXEC_MODE", "docker")
    monkeypatch.setattr(E.subprocess, "run", run)
    return calls


def test_docker_sandbox_container_is_actually_locked_down(monkeypatch):
    calls = _fake_docker(monkeypatch)
    assert E.check("```python\nprint(1)\n```")["status"] == "ok"
    cmd = calls["cmd"]
    for flag in (["--network", "none"], ["--read-only"], ["--cap-drop", "ALL"],
                 ["--user", "65534:65534"], ["--security-opt", "no-new-privileges"]):
        joined = " ".join(cmd)
        assert " ".join(flag) in joined, f"missing {flag} in {cmd}"
    assert "--pids-limit" in cmd and "--memory" in cmd
    assert "-v" not in cmd and "--volume" not in cmd            # nothing mounted in
    assert cmd[-4:] == [E.SANDBOX_IMAGE, "python", "-m", "optimize.sandbox_driver"]


def test_docker_sandbox_runtime_flag_enables_gvisor(monkeypatch):
    calls = _fake_docker(monkeypatch)
    monkeypatch.setattr(E, "SANDBOX_RUNTIME", "runsc")
    E.check("```python\nprint(1)\n```")
    cmd = " ".join(calls["cmd"])
    assert "--runtime runsc" in cmd


def test_docker_sandbox_maps_driver_phases_to_statuses(monkeypatch):
    _fake_docker(monkeypatch,
                 stdout='{"phase": "assertion", "returncode": 1, "stderr": "AssertionError: wrong content", "timeout": false}')
    r = E.check_with_fixture(ANSWER_OK, CHECK["fixture"], CHECK["assert"])
    assert r["status"] == "assert_failed" and "wrong content" in r["detail"]
    _fake_docker(monkeypatch,
                 stdout='{"phase": "code", "returncode": 1, "stderr": "ModuleNotFoundError: no pdf lib", "timeout": false}')
    assert E.check_with_fixture(ANSWER_OK, "", "")["status"] == "inconclusive"
    _fake_docker(monkeypatch,
                 stdout='{"phase": "fixture", "returncode": 1, "stderr": "boom", "timeout": false}')
    assert E.check_with_fixture(ANSWER_OK, "x=1", "")["status"] == "fixture_error"
    _fake_docker(monkeypatch,
                 stdout='{"phase": "code", "returncode": -1, "stderr": "", "timeout": true}')
    assert E.check_with_fixture(ANSWER_OK, "", "")["status"] == "inconclusive"
    _fake_docker(monkeypatch,
                 stdout='{"phase": "code", "returncode": 1, "stderr": "NameError: nope", "timeout": false}')
    assert E.check("```python\nprint(1)\n```")["status"] == "code_error"


def test_docker_sandbox_fails_closed_when_unavailable(monkeypatch):
    # no docker CLI at all -> inconclusive everywhere, and the judge note stays silent;
    # never a bare-subprocess fallback
    _fake_docker(monkeypatch, raise_exc=FileNotFoundError("docker"))
    r = E.check("```python\nprint(1)\n```")
    assert r["status"] == "runtime_error" and "unavailable" in r["detail"]
    r = E.check_with_fixture(ANSWER_OK, CHECK["fixture"], CHECK["assert"])
    assert r["status"] == "inconclusive" and "unavailable" in r["detail"]
    assert E.judge_note(ANSWER_OK, "task", check_spec=CHECK) == ""
    # daemon errors (nonzero docker exit) fail closed the same way
    _fake_docker(monkeypatch, stdout="", returncode=125)
    assert E.check_with_fixture(ANSWER_OK, "", "")["status"] == "inconclusive"


def test_check_specs_do_not_execute_without_optin(monkeypatch):
    # fail closed by default: no EXEC_SANDBOX -> check: specs are inconclusive and silent,
    # and nothing is ever executed
    monkeypatch.setattr(E, "EXEC_MODE", "")
    monkeypatch.setattr(E, "EXEC_SANDBOX", False)

    def forbidden(*a, **k):
        raise AssertionError("executed code without an EXEC_SANDBOX opt-in")

    monkeypatch.setattr(E.subprocess, "run", forbidden)
    r = E.check_with_fixture(ANSWER_OK, CHECK["fixture"], CHECK["assert"])
    assert r["status"] == "inconclusive" and "execution disabled" in r["detail"]
    assert E.judge_note(ANSWER_OK, "task", check_spec=CHECK) == ""
    assert E.check("```python\nprint(1)\n```")["status"] == "ok"    # static path still works


def test_sandbox_driver_end_to_end_without_docker(tmp_path):
    # the driver is plain python — exercise the real phase pipeline directly
    import json as _json
    spec = {"fixture": CHECK["fixture"], "code": 'text = open("input.txt").read()\n'
            'open("output.txt", "w").write(text.upper())', "assertion": CHECK["assert"]}
    run = subprocess.run([sys.executable, "-m", "optimize.sandbox_driver"],
                         input=_json.dumps(spec), capture_output=True, text=True,
                         cwd=str(tmp_path), env={**os.environ, "PYTHONPATH": os.getcwd()})
    assert _json.loads(run.stdout) == {"ok": True}
    bad = {**spec, "code": spec["code"].replace(".upper()", ".lower()")}
    run = subprocess.run([sys.executable, "-m", "optimize.sandbox_driver"],
                         input=_json.dumps(bad), capture_output=True, text=True,
                         cwd=str(tmp_path), env={**os.environ, "PYTHONPATH": os.getcwd()})
    verdict = _json.loads(run.stdout)
    assert verdict["phase"] == "assertion" and "AssertionError" in verdict["stderr"]


def test_docker_sandbox_payload_and_unique_names(monkeypatch):
    import json as _json
    calls_a = _fake_docker(monkeypatch)
    E.check_with_fixture(ANSWER_OK, CHECK["fixture"], CHECK["assert"], timeout=7)
    payload = _json.loads(calls_a["input"])
    assert payload["fixture"] == CHECK["fixture"] and payload["assertion"] == CHECK["assert"]
    assert "output.txt" in payload["code"] and payload["timeout"] == 7
    name_a = calls_a["cmd"][calls_a["cmd"].index("--name") + 1]
    calls_b = _fake_docker(monkeypatch)
    E.check("```python\nprint(1)\n```")
    name_b = calls_b["cmd"][calls_b["cmd"].index("--name") + 1]
    assert name_a != name_b and name_a.startswith("ingot-sandbox-")


def test_docker_sandbox_outer_timeout_kills_the_container(monkeypatch):
    kills = []

    def run(cmd, **kwargs):
        if cmd[:2] == ["docker", "kill"]:
            kills.append(cmd[2])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise subprocess.TimeoutExpired(cmd="docker", timeout=1)

    monkeypatch.setattr(E, "EXEC_MODE", "docker")
    monkeypatch.setattr(E.subprocess, "run", run)
    assert E.check_with_fixture(ANSWER_OK, "", "")["status"] == "inconclusive"
    assert len(kills) == 1 and kills[0].startswith("ingot-sandbox-")


def test_docker_sandbox_garbage_output_fails_closed(monkeypatch):
    _fake_docker(monkeypatch, stdout="not json at all")
    assert E.check_with_fixture(ANSWER_OK, "", "")["status"] == "inconclusive"
    _fake_docker(monkeypatch, stdout="")
    assert E.check("```python\nprint(1)\n```")["status"] == "runtime_error"
