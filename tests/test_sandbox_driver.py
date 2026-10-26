"""Driver phase order and fail-closed verdicts, with real child interpreters."""
import io
import json

import pytest

from optimize import sandbox_driver as driver


@pytest.mark.parametrize(('spec', 'expected'), [
    ({'fixture': "open('value', 'w').write('fixture')",
      'code': "assert open('value').read() == 'fixture'; open('value', 'w').write('code')",
      'assertion': "assert open('value').read() == 'code'"}, {'ok': True}),
    ({'fixture': "raise RuntimeError('fixture failed')", 'code': "open('unexpected', 'w').close()"},
     {'phase': 'fixture', 'returncode': 1, 'timeout': False}),
    ({'code': 'while True: pass', 'timeout': 1},
     {'phase': 'code', 'returncode': -1, 'stderr': '', 'timeout': True}),
    ({'assertion': 'assert False, "wrong answer"'},
     {'phase': 'assertion', 'returncode': 1, 'timeout': False}),
])
def test_main_sequences_phases_and_reports_first_failure(spec, expected, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(spec)))
    driver.main()
    result = json.loads(capsys.readouterr().out)
    for key, value in expected.items():
        assert result[key] == value
    if result.get('returncode') == 1:
        assert 'Traceback' in result['stderr']
    assert not (tmp_path / 'unexpected').exists()


def test_phase_bounds_stderr(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = driver._phase('code', "import sys; sys.stderr.write('x' * 3000); sys.exit(7)", 5)
    assert result == {'phase': 'code', 'returncode': 7, 'stderr': 'x' * 2000, 'timeout': False}
