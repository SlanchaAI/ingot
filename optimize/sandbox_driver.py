"""Runs INSIDE the throwaway sandbox container (see execcheck._sandbox): reads a JSON spec
{fixture, code, assertion, timeout} on stdin, runs each present phase as its own subprocess in the
scratch workdir, and prints a single JSON verdict on stdout — {"ok": true}, or the first failing
phase as {"phase", "returncode", "stderr", "timeout"}. This file is trusted repo code; the
untrusted text is only ever executed as a child process, never exec()'d in this interpreter.
The container provides the containment (no network, no mounts, dropped caps, nobody user,
memory/pid limits); this driver just sequences the phases and enforces the per-phase timeout."""
import json
import subprocess
import sys
import tempfile


def _phase(name: str, source: str, timeout: int) -> dict | None:
    """None when the phase exits 0; the failure verdict otherwise."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=".") as f:
        f.write(source)
        path = f.name
    try:
        run = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"phase": name, "returncode": -1, "stderr": "", "timeout": True}
    if run.returncode != 0:
        return {"phase": name, "returncode": run.returncode,
                "stderr": run.stderr[-2000:], "timeout": False}
    return None


def main() -> None:
    spec = json.load(sys.stdin)
    timeout = int(spec.get("timeout", 15))
    for name in ("fixture", "code", "assertion"):
        source = spec.get(name, "")
        if source:
            failed = _phase(name, source, timeout)
            if failed:
                print(json.dumps(failed))
                return
    print(json.dumps({"ok": True}))


if __name__ == "__main__":
    main()
