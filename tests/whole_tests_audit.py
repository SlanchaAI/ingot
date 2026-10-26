"""Inventory reconciliation and execution evidence, used by verify_whole_tests.sh.

The checked-in map is explicit. Discovery is independent, so a new source module fails
until its behavior tests (or an individual justified exclusion) are recorded.
"""
import ast
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


class InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.active = False

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            assert not dict(attrs).get('src'), 'External scripts require separate inventory'
            self.active = True
            self.scripts.append('')

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.scripts[-1] += data


def inventory():
    tracked = subprocess.check_output(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT)
    paths = set()
    for name in tracked.decode().split('\0'):
        if not name:
            continue
        path = ROOT / name
        if path.suffix in {'.py', '.sh', '.js', '.cjs', '.mjs', '.html'}:
            paths.add(name)
        elif path.is_file() and path.read_bytes()[:2] == b'#!':
            paths.add(name)
    return paths


def prepare(destination):
    mapping = json.loads((ROOT / 'tests/whole_tests_inventory.json').read_text())
    discovered = inventory()
    assert discovered == set(mapping), (
        f'Unmapped modules: {sorted(discovered - mapping.keys())}; '
        f'stale entries: {sorted(mapping.keys() - discovered)}')
    for path, entry in mapping.items():
        if 'exclude' in entry:
            assert entry['exclude'].strip(), path
        else:
            assert entry['tests'], f'No behavior mapping: {path}'
            for test in entry['tests']:
                assert (ROOT / test).is_file(), f'{path}: missing test {test}'
    parser = InlineScripts()
    parser.feed((ROOT / 'ui/static/index.html').read_text())
    (destination / 'inline-scripts.json').write_text(json.dumps(parser.scripts))
    (destination / 'inventory.json').write_text(json.dumps(mapping, indent=2) + '\n')
    print(f'Inventory: {len(mapping)} files, '
          f'{sum("exclude" not in entry for entry in mapping.values())} executable modules')
    for path, entry in sorted(mapping.items()):
        print(path + ': ' + (entry.get('exclude') or ', '.join(entry['tests'])))
    return mapping


def audit(destination):
    mapping = prepare(destination)
    report = json.loads((destination / 'coverage.json').read_text())['files']
    junit = ET.parse(destination / 'pytest.xml')
    cases = junit.findall('.//testcase')
    assert cases, 'No collected tests'
    assert not junit.findall('.//skipped'), 'Skipped tests are unmet criteria'
    assert not junit.findall('.//failure') and not junit.findall('.//error'), 'Failed tests'
    passed_modules = {case.attrib['classname'].split('.')[-1] for case in cases}
    trace = (destination / 'shell.trace').read_text()
    evidence = {}
    for path, entry in mapping.items():
        if 'exclude' in entry:
            continue
        if path.endswith('.py'):
            assert path in report, f'Coverage omitted {path}'
            data = report[path]
            tree = ast.parse((ROOT / path).read_text())
            # Import execution is insufficient: require an executed statement inside a function,
            # attributed to one of the mapped tests by coverage's dynamic test contexts.
            behavior_lines = {node.lineno for fn in ast.walk(tree)
                              if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                              for node in ast.walk(fn)
                              if isinstance(node, (ast.Return, ast.Raise, ast.Assign, ast.If,
                                                   ast.For, ast.With, ast.Assert, ast.Expr))
                              and not (isinstance(node, ast.Expr) and
                                       isinstance(node.value, ast.Constant))}
            executed = behavior_lines.intersection(data['executed_lines'])
            contexts = {context for line in executed
                        for context in data.get('contexts', {}).get(str(line), [])}
            assert executed, f'No function behavior exercised: {path}'
            mapped = {Path(test).stem for test in entry['tests']}
            assert any(any(part in context.split('.') for part in mapped)
                       for context in contexts), f'No mapped test executed behavior: {path}'
            assert mapped.intersection(passed_modules), f'No mapped tests passed: {path}'
            evidence[path] = {'executed_behavior_lines': len(executed),
                              'line_percent': data['summary']['percent_covered']}
        elif path.startswith('scripts/'):
            # Shell xtrace includes source and line, including copied fixture scripts.
            name = Path(path).name
            lines = set()
            for line in trace.splitlines():
                source, separator, rest = line.lstrip('+').partition(':')
                number, _, _ = rest.partition(':')
                if separator and Path(source).name == name and number.isdecimal():
                    lines.add(int(number))
            assert len(lines) >= 5, f'No meaningful shell execution trace: {path}'
            assert any(Path(test).stem in passed_modules for test in entry['tests']), path
            evidence[path] = {'executed_shell_lines': sorted(lines)}
        elif path.endswith('.html'):
            coverage = json.loads((destination / 'browser-coverage.json').read_text())
            scripts = json.loads((destination / 'inline-scripts.json').read_text())
            assert len(coverage) == len(scripts), 'Inline script omitted from coverage'
            for script in coverage:
                assert any(r['count'] > 0 for f in script['functions'] for r in f['ranges']), script['url']
            assert sum(f['ranges'][0]['count'] > 0 for script in coverage
                       for f in script['functions']) >= 15, 'Browser behavior not exercised'
            evidence[path] = {'scripts': len(coverage), 'functions': sum(
                f['ranges'][0]['count'] > 0 for script in coverage for f in script['functions'])}
        else:
            raise AssertionError(f'Unsupported executable language: {path}')
    (destination / 'audit.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(f'Audit passed: {len(cases)} tests, zero skips; '
          f'{len(evidence)} modules have runtime evidence; zero unexplained missing modules')


if __name__ == '__main__':
    destination = Path(sys.argv[2])
    {'inventory': prepare, 'audit': audit}[sys.argv[1]](destination)
