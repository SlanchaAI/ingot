"""Unit tests for the deterministic acceptance-criteria gate (no network/LLM)."""
import re

import pytest

from optimize.acceptance import evaluate, load_criteria


def test_load_criteria_parses_forbid_and_skips_empty(tmp_path):
    (tmp_path / "pdf.yaml").write_text(
        "skill: pdf\n"
        "acceptance:\n"
        "- id: no_init\n"
        "  forbid: 'tailwindcss init'\n"
        "  description: v4 has no init\n"
        "- id: blank\n"          # no forbid key -> skipped
        "  description: ignored\n")
    criteria = load_criteria("pdf", tmp_path)
    assert [c["id"] for c in criteria] == ["no_init"]
    assert criteria[0]["forbid"].search("run tailwindcss init now")


def test_load_criteria_missing_file_and_no_block_are_empty(tmp_path):
    assert load_criteria("absent", tmp_path) == []
    (tmp_path / "pdf.yaml").write_text("skill: pdf\ntrain: []\n")
    assert load_criteria("pdf", tmp_path) == []


def test_load_criteria_raises_on_bad_regex(tmp_path):
    # a safety invariant that silently stops firing is worse than a loud failure
    (tmp_path / "pdf.yaml").write_text("skill: pdf\nacceptance:\n- id: bad\n  forbid: '('\n")
    with pytest.raises(re.error):
        load_criteria("pdf", tmp_path)


def test_evaluate_flags_forbidden_pattern_and_counts_hits():
    criteria = [{"id": "no_v3", "forbid": re.compile(r"@tailwind\s+base"), "description": "v3 directive"}]
    answers = ["use @import \"tailwindcss\";", "add @tailwind base;", "@tailwind  base again"]
    reasons = evaluate(criteria, answers)
    assert len(reasons) == 1
    assert "no_v3" in reasons[0] and "2/3" in reasons[0] and "v3 directive" in reasons[0]


def test_evaluate_clean_answers_pass():
    criteria = [{"id": "no_init", "forbid": re.compile(r"tailwindcss init"), "description": ""}]
    assert evaluate(criteria, ["@import \"tailwindcss\";", ""]) == []


def test_evaluate_no_criteria_is_noop():
    assert evaluate([], ["anything at all"]) == []
