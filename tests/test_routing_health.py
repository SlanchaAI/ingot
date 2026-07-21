"""Library-wide routing health check: suite replay, parity, collision scan, exit semantics.
No embeddings, no LLM: routers are scripted or monkeypatched."""
from types import SimpleNamespace

import yaml

from optimize import routing_health as H


class _ScriptedRouter:
    """route() answers from a task -> match script, tolerant of the eval helpers' kwargs."""

    def __init__(self, script, per_harness=None):
        self._script = script
        self._per_harness = per_harness or {}

    def route(self, task, harness="codex", **kwargs):
        if task in self._per_harness:
            return {"match": self._per_harness[task].get(harness), "alternatives": []}
        return {"match": self._script.get(task), "alternatives": []}

    def nearest(self, description):
        return "", 0.0


def _write_suite(tasks_dir, skill, cases):
    (tasks_dir / f"{skill}.yaml").write_text(yaml.safe_dump({"routing": cases}))


def test_load_routing_cases_reads_suite_and_tolerates_absence(tmp_path):
    _write_suite(tmp_path, "pdf", [{"task": "merge pdfs", "expected": "pdf"}])
    assert H.load_routing_cases("pdf", tmp_path) == [{"task": "merge pdfs", "expected": "pdf"}]
    assert H.load_routing_cases("nope", tmp_path) == []


def test_check_suites_passes_a_healthy_suite(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "TASKS_DIR", tmp_path)
    _write_suite(tmp_path, "pdf", [
        {"task": "merge pdfs", "expected": "pdf"},
        {"task": "hello", "expected": None},
    ])
    router = _ScriptedRouter({"merge pdfs": "pdf", "hello": None})
    assert H.check_suites(router, ["pdf"], log=lambda *_: None) == []


def test_check_suites_flags_misroutes_no_route_and_parity(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "TASKS_DIR", tmp_path)
    _write_suite(tmp_path, "pdf", [
        {"task": "merge pdfs", "expected": "pdf"},          # misroutes to docx
        {"task": "hello", "expected": None},                # own skill over-triggers
        {"task": "make a slide deck", "expected": None},    # another skill claims it: fine
        {"task": "fill a form", "expected": "pdf", "parity": True},  # harness-dependent
    ])
    router = _ScriptedRouter(
        {"merge pdfs": "docx", "hello": "pdf", "make a slide deck": "pptx"},
        per_harness={"fill a form": {"claude": "pdf", "codex": "docx"}})
    problems = H.check_suites(router, ["pdf"], log=lambda *_: None)
    assert any("expected pdf but routed to docx" in p for p in problems)
    assert any("expected no route but routed to pdf" in p for p in problems)
    assert any("parity" in p for p in problems)
    # a no-route negative claimed by a DIFFERENT skill is the library working, not a problem
    assert not any("slide deck" in p for p in problems)


def test_check_suites_skips_skill_without_cases(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "TASKS_DIR", tmp_path)
    (tmp_path / "pdf.yaml").write_text(yaml.safe_dump({"train": [{"task": "t", "rubric": "r"}]}))
    assert H.check_suites(_ScriptedRouter({}), ["pdf"], log=lambda *_: None) == []


def test_check_collisions_reports_each_pair_once(monkeypatch):
    skills = [SimpleNamespace(name="pdf", description="merge pdfs"),
              SimpleNamespace(name="pdf-forms", description="merge pdf forms"),
              SimpleNamespace(name="xlsx", description="excel spreadsheets")]
    monkeypatch.setattr(H, "load_skills", lambda: skills)

    class _FakeRouter:
        def __init__(self, others):
            self._others = others

        def nearest(self, description):
            # pdf and pdf-forms collide with each other, xlsx is distinct
            if description in ("merge pdfs", "merge pdf forms"):
                other = "pdf-forms" if description == "merge pdfs" else "pdf"
                return other, 0.97
            return "pdf", 0.10

    monkeypatch.setattr(H, "Router", _FakeRouter)
    problems = H.check_collisions(log=lambda *_: None)
    assert len(problems) == 1  # the colliding pair is reported once, not once per direction
    assert "'pdf'" in problems[0] and "'pdf-forms'" in problems[0]


def test_run_health_aggregates_and_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "TASKS_DIR", tmp_path)
    _write_suite(tmp_path, "pdf", [{"task": "merge pdfs", "expected": "pdf"}])
    monkeypatch.setattr(H, "load_skills", lambda: [SimpleNamespace(name="pdf", description="d")])
    monkeypatch.setattr(H, "Router", lambda skills: _ScriptedRouter({"merge pdfs": "pdf"}))
    lines = []
    assert H.run_health(log=lines.append) == []
    assert any("routing healthy" in line for line in lines)

    monkeypatch.setattr(H, "Router", lambda skills: _ScriptedRouter({"merge pdfs": None}))
    problems = H.run_health(log=lines.append)
    assert problems and "expected pdf" in problems[0]
