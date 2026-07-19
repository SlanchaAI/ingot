"""Unit tests for the per-skill load counter (no server needed)."""
from mcp_server import usage_counts


def test_record_use_increments_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_counts, "_PATH", tmp_path / "skill_usage.json")
    assert usage_counts.load_counts() == {}          # nothing written yet
    usage_counts.record_use("pdf")
    usage_counts.record_use("pdf")
    usage_counts.record_use("docx")
    assert usage_counts.load_counts() == {"pdf": 2, "docx": 1}


def test_record_use_ignores_empty_name(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_counts, "_PATH", tmp_path / "skill_usage.json")
    usage_counts.record_use("")
    usage_counts.record_use(None)
    assert usage_counts.load_counts() == {}


def test_load_counts_tolerates_garbage(tmp_path, monkeypatch):
    path = tmp_path / "skill_usage.json"
    monkeypatch.setattr(usage_counts, "_PATH", path)
    path.write_text("not json{")
    assert usage_counts.load_counts() == {}           # unreadable -> empty, never raises
    path.write_text('["a", "list"]')
    assert usage_counts.load_counts() == {}           # wrong type -> empty
