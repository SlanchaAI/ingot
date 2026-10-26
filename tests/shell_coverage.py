"""Record real shell line execution without mixing traces into CLI output."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.fixture(autouse=True)
def shell_trace(monkeypatch):
    destination = os.environ.get('WHOLE_SHELL_TRACE')
    if not destination:
        return
    original = subprocess.run

    def traced(args, *positional, **kwargs):
        if isinstance(args, (list, tuple)):
            script = next((str(a) for a in args[:3] if str(a).endswith('.sh')), None)
            if script and Path(script).is_file():
                command = list(args)
                if command[0] == script:
                    command = ['bash', '-x', *command]
                elif command[0] in ('bash', 'sh'):
                    command[0:1] = ['bash', '-x']
                with open(destination, 'a') as trace:
                    env = dict(kwargs.get('env', os.environ))
                    env.update(BASH_XTRACEFD=str(trace.fileno()), PS4='+${BASH_SOURCE}:${LINENO}: ')
                    kwargs.update(env=env, pass_fds=(*kwargs.get('pass_fds', ()), trace.fileno()))
                    return original(command, *positional, **kwargs)
        return original(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, 'run', traced)
