"""Unit tests for auto-drafted eval task sets (ingot.optimize.draft), LLM mocked."""
import json

import pytest

from ingot.optimize import draft as D


class _FakeMsg:
    def __init__(self, content):
        self.content = content
        self.usage_metadata = None


def _mock_llm(monkeypatch, tasks):
    payload = json.dumps({"tasks": tasks})
    monkeypatch.setattr(D, "_llm", lambda: type("L", (), {"invoke": lambda self, p: _FakeMsg(payload)})())


def test_draft_splits_evenly_into_train_and_holdout(monkeypatch):
    tasks = [{"task": f"task {i}", "rubric": f"rubric {i}"} for i in range(8)]
    _mock_llm(monkeypatch, tasks)
    out = D.draft_tasks("pdf", "desc", "body", n=8)
    assert len(out["train"]) == 4 and len(out["holdout"]) == 4
    train_texts = {t["task"] for t in out["train"]}
    holdout_texts = {t["task"] for t in out["holdout"]}
    assert train_texts.isdisjoint(holdout_texts)                 # no leakage between splits


def test_draft_drops_malformed_task_entries(monkeypatch):
    tasks = [{"task": "ok1", "rubric": "r"}, {"nope": "x"}, {"task": "ok2"}, {"task": "ok3"}, {"task": "ok4"}]
    _mock_llm(monkeypatch, tasks)
    out = D.draft_tasks("pdf", "d", "b", n=5)
    all_tasks = out["train"] + out["holdout"]
    assert all(t.get("task") for t in all_tasks) and len(all_tasks) == 4  # the entry without a task is dropped


def test_draft_raises_if_too_few_usable_tasks(monkeypatch):
    _mock_llm(monkeypatch, [{"task": "only one", "rubric": "r"}])
    with pytest.raises(SystemExit):
        D.draft_tasks("pdf", "d", "b", n=8)


def test_draft_carries_a_weighted_checklist_per_task(monkeypatch):
    """The checklist is what gives a task more than one measurement, so a drafted set has to carry
    it -- otherwise only hand-written tasks ever get graded finely."""
    check = {"id": "handles_empty", "criterion": "Handles an empty input file without raising.",
             "weight": 4, "dimension": "completeness"}
    _mock_llm(monkeypatch, [{"task": f"t{i}", "rubric": "r", "checklist": [check]} for i in range(4)])
    out = D.draft_tasks("pdf", "d", "b", n=4)
    assert out["train"][0]["checklist"] == [check]


@pytest.mark.parametrize("bad, why", [
    ({"id": "Has Spaces", "criterion": "a valid criterion here"}, "id is not snake_case"),
    ({"id": "ok_id", "criterion": "short"}, "criterion too short to grade"),
    ("not a dict", "not an object"),
])
def test_draft_drops_ungradeable_checks(monkeypatch, bad, why):
    good = {"id": "keeps_this", "criterion": "A criterion long enough to grade.", "weight": 2,
            "dimension": "correctness"}
    _mock_llm(monkeypatch, [{"task": f"t{i}", "rubric": "r", "checklist": [good, bad]}
                            for i in range(4)])
    out = D.draft_tasks("pdf", "d", "b", n=4)
    assert [c["id"] for c in out["train"][0]["checklist"]] == ["keeps_this"], why


def test_draft_deduplicates_check_ids(monkeypatch):
    """Two checks with one id would collapse in the judge's JSON response, silently dropping a
    check while its weight still counted against the total."""
    dup = [{"id": "same", "criterion": "The first criterion text."},
           {"id": "same", "criterion": "A different criterion, same id."}]
    _mock_llm(monkeypatch, [{"task": f"t{i}", "rubric": "r", "checklist": dup} for i in range(4)])
    assert len(D.draft_tasks("pdf", "d", "b", n=4)["train"][0]["checklist"]) == 1


def test_draft_clamps_weights_and_defaults_unknown_dimensions(monkeypatch):
    wild = {"id": "wild", "criterion": "A criterion long enough to grade.", "weight": 99,
            "dimension": "vibes"}
    _mock_llm(monkeypatch, [{"task": f"t{i}", "rubric": "r", "checklist": [wild]} for i in range(4)])
    got = D.draft_tasks("pdf", "d", "b", n=4)["train"][0]["checklist"][0]
    assert got["weight"] == 5 and got["dimension"] == "correctness"


def test_draft_tolerates_a_task_with_no_checklist(monkeypatch):
    """judge() falls back to its default checklist, so a missing one degrades to four dimensions
    rather than failing the draft."""
    _mock_llm(monkeypatch, [{"task": f"t{i}", "rubric": "r"} for i in range(4)])
    assert D.draft_tasks("pdf", "d", "b", n=4)["train"][0]["checklist"] == []


def _mock_routing_llm(monkeypatch, positive, negative):
    payload = json.dumps({"positive": positive, "negative": negative})
    monkeypatch.setattr(D, "_llm", lambda: type("L", (), {"invoke": lambda self, p: _FakeMsg(payload)})())


def test_draft_routing_cases_shape(monkeypatch):
    _mock_routing_llm(monkeypatch, ["sum a column", "lookup a value", "fix my formula", "date math"],
                      ["convert xlsx to csv", "thanks!"])
    cases = D.draft_routing_cases("excel-formulas", "desc", "body")
    positives = [c for c in cases if c["expected"] == "excel-formulas"]
    negatives = [c for c in cases if c["expected"] is None]
    assert len(positives) == 4 and len(negatives) == 2
    assert positives[0]["parity"] is True and negatives[0]["parity"] is True
    assert positives[1]["harness"] == "claude"            # cross-harness coverage
    assert all("parity" not in c for c in positives[2:])


def test_draft_routing_cases_rejects_thin_output(monkeypatch):
    _mock_routing_llm(monkeypatch, ["only one"], [])
    with pytest.raises(SystemExit, match="need at least"):
        D.draft_routing_cases("excel-formulas", "desc", "body")


def test_draft_and_append_routing_preserves_existing_tasks(monkeypatch, tmp_path):
    import yaml
    _mock_routing_llm(monkeypatch, ["a", "b"], ["c"])
    (tmp_path / "sk.yaml").write_text("skill: sk\ntrain:\n- task: t\n  rubric: r\n")
    D.draft_and_append_routing("sk", "desc", "body", tmp_path)
    data = yaml.safe_load((tmp_path / "sk.yaml").read_text())
    assert data["train"] == [{"task": "t", "rubric": "r"}]        # untouched
    assert len(data["routing"]) == 3 and data["routing"][2]["expected"] is None
