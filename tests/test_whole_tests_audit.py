"""The verifier must reject missing inventory, skipped suites and import-only coverage."""
import json

import pytest

from tests import whole_tests_audit as audit


def test_inventory_rejects_new_unmapped_module(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, 'inventory', lambda: {'new_module.py'})
    with pytest.raises(AssertionError, match='Unmapped modules'):
        audit.prepare(tmp_path)


@pytest.mark.parametrize('xml, message', [
    ('<testsuite><testcase classname="tests.test_example"><skipped/></testcase></testsuite>', 'Skipped'),
    ('<testsuite><testcase classname="tests.test_example"><failure/></testcase></testsuite>', 'Failed'),
    ('<testsuite/>', 'No collected'),
])
def test_audit_rejects_incomplete_suites(tmp_path, monkeypatch, xml, message):
    monkeypatch.setattr(audit, 'prepare', lambda _: {})
    (tmp_path / 'coverage.json').write_text('{"files":{}}')
    (tmp_path / 'pytest.xml').write_text(xml)
    with pytest.raises(AssertionError, match=message):
        audit.audit(tmp_path)


@pytest.mark.parametrize('coverage, message', [
    ({}, 'Coverage omitted'),
    ({'example.py': {'executed_lines': [1], 'contexts': {}}}, 'No function behavior'),
    ({'example.py': {'executed_lines': [3], 'contexts': {'3': ['test_other.test_x']}}}, 'No mapped test'),
])
def test_audit_rejects_omitted_import_only_or_unmapped_execution(tmp_path, monkeypatch, coverage, message):
    monkeypatch.setattr(audit, 'ROOT', tmp_path)
    monkeypatch.setattr(audit, 'prepare', lambda _: {'example.py': {'tests': ['tests/test_example.py']}})
    (tmp_path / 'example.py').write_text('import os\ndef behavior():\n    return 7\n')
    (tmp_path / 'coverage.json').write_text(json.dumps({'files': coverage}))
    (tmp_path / 'pytest.xml').write_text('<testsuite><testcase classname="tests.test_example"/></testsuite>')
    (tmp_path / 'shell.trace').write_text('')
    with pytest.raises(AssertionError, match=message):
        audit.audit(tmp_path)
