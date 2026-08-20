"""Unit tests for reading a harness x model matrix for display.

The rules under test are the ones the first live matrix broke: a combination that did not run must
not reach a reader as a zero, every lift must carry the `n` behind it, and a matrix whose controls
sit at the ceiling must report the ceiling rather than a winner."""
import json
import importlib
from pathlib import Path

import pytest

from ingot.optimize import harbor_report as R
from ingot import paths

FIXTURES = Path(__file__).parent / "fixtures" / "harbor"


def test_default_matrix_root_uses_the_mutable_state_directory(monkeypatch, tmp_path):
    with monkeypatch.context() as isolated:
        isolated.setenv(paths.HOME, str(tmp_path / "state"))
        importlib.reload(R)
        assert R.HARBOR_DIR == tmp_path / "state" / "runs" / "harbor"
    importlib.reload(R)


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


LIVE = {"skill": "demo", "judge": "google/gemini-2.5-flash",
        "harnesses": {
            "claude-code@anthropic/claude-opus-5": {
                "skill_mean": 0.75, "control_mean": 0.5, "lift": 0.25,
                "harness": "claude-code", "model": "anthropic/claude-opus-5",
                "tasks_scored": 4, "tasks_dropped": []},
            "aider@openai/gpt-5.5": {
                "error": "RuntimeError: every task returned an empty workspace",
                "harness": "aider", "model": "openai/gpt-5.5"}}}


def test_a_combination_that_did_not_run_carries_no_lift(tmp_path):
    """The whole point. A blank or a 0.0 in the lift column reads as 'measured, no effect', which
    is the opposite of what happened, and is how a crashed control arm became 'lift +0.750'."""
    _write(tmp_path, "demo.json", LIVE)
    rows = {r["combination"]: r for r in R.read_matrix("demo", tmp_path)["rows"]}
    broken = rows["aider@openai/gpt-5.5"]
    assert "lift" not in broken and "skill_mean" not in broken and "n" not in broken
    assert "empty workspace" in broken["error"]


def test_every_measured_row_carries_the_n_behind_it(tmp_path):
    _write(tmp_path, "demo.json", LIVE)
    measured = [r for r in R.read_matrix("demo", tmp_path)["rows"] if "lift" in r]
    assert [r["n"] for r in measured] == [4]


def test_scale_provenance_is_allowlisted_on_measured_and_unmeasured_rows(tmp_path):
    provenance = {"target_alias": "qwen35-4b", "family": "Qwen3.5", "parameter_billions": 4.0,
                  "quantization": "fp8-load", "tool_parser": "qwen3_coder"}
    _write(tmp_path, "demo.rescored.json", {"combinations": {
        "aider@qwen35-4b--qwen35-4b-deadbeef": {
            **provenance, "harness": "aider", "model": "qwen35-4b", "lift": 0.2,
            "skill_mean": 0.6, "control_mean": 0.4, "tasks_scored": 4, "attempts": 1,
            "private_note": "must not escape"},
        "pi@qwen35-4b--qwen35-4b-deadbeef": {
            **provenance, "harness": "pi", "model": "qwen35-4b", "error": "canary failed",
            "private_note": "must not escape"},
    }})
    rows = R.read_matrix("demo", tmp_path)["rows"]
    assert len(rows) == 2
    assert all({key: row[key] for key in provenance} == provenance for row in rows)
    assert {row["model"] for row in rows} == {"Qwen/Qwen3.5-4B"}
    assert all("private_note" not in row for row in rows)
    assert "lift" not in next(row for row in rows if row["harness"] == "pi")


def test_qwen_name_requires_matching_alias_and_family(tmp_path):
    _write(tmp_path, "demo.json", {"harnesses": {"a@wire-id": {
        "harness": "a", "model": "wire-id", "target_alias": "qwen35-4b",
        "family": "Qwen3.6", "error": "not measured",
    }}})

    assert R.read_matrix("demo", tmp_path)["rows"][0]["model"] == "wire-id"


@pytest.mark.parametrize("value", [True, "4", 0, -1, float("inf")])
def test_invalid_parameter_counts_do_not_reach_the_size_chart_payload(tmp_path, value):
    _write(tmp_path, "demo.json", {"harnesses": {"a@m": {
        "harness": "a", "model": "m", "parameter_billions": value,
        "lift": 0.1, "skill_mean": 0.5, "control_mean": 0.4,
        "tasks_scored": 4, "attempts": 1}}})

    row = R.read_matrix("demo", tmp_path)["rows"][0]
    assert "parameter_billions" not in row


def test_qwen_size_fixture_exposes_only_observed_scale_points():
    matrix = R.read_matrix("qwen-size-matrix", FIXTURES)

    assert matrix["models"] == ["dot-backbone", "qwen35-0.8b", "qwen35-2b", "qwen35-4b", "qwen35-9b"]
    assert len(matrix["rows"]) == 8
    assert matrix["measured"] == 7 and matrix["unmeasured"] == 1
    assert all(row["parameter_billions"] in {0.8, 2.0, 4.0, 9.0, 27.0} for row in matrix["rows"])


def test_the_broken_row_is_counted_but_not_averaged(tmp_path):
    _write(tmp_path, "demo.json", LIVE)
    out = R.read_matrix("demo", tmp_path)
    assert (out["measured"], out["unmeasured"]) == (1, 1)
    assert out["mean_lift"] == 0.25   # not 0.125, which is what averaging the failure would give


def test_a_ceilinged_matrix_reports_the_ceiling_instead_of_a_winner(tmp_path):
    """Controls near the top of the scale leave less headroom than the judge's own run-to-run
    spread. Naming a best combination off that is reporting noise as a result."""
    _write(tmp_path, "demo.json", {"harnesses": {
        "a@m": {"lift": 0.02, "skill_mean": 0.87, "control_mean": 0.85, "tasks_scored": 4},
        "b@m": {"lift": -0.03, "skill_mean": 0.82, "control_mean": 0.85, "tasks_scored": 4}}})
    out = R.summarize(R.read_matrix("demo", tmp_path)["rows"])
    assert out["best"] is None
    assert "ceiling" in out["warning"] and "too easy" in out["warning"]


def test_a_matrix_with_headroom_does_name_the_best_combination(tmp_path):
    _write(tmp_path, "demo.json", {"harnesses": {
        "a@m": {"lift": 0.25, "skill_mean": 0.75, "control_mean": 0.50, "tasks_scored": 4,
                "attempts": 3},
        "b@m": {"lift": 0.05, "skill_mean": 0.55, "control_mean": 0.50, "tasks_scored": 4,
                "attempts": 3}}})
    out = R.summarize(R.read_matrix("demo", tmp_path)["rows"])
    assert out["warning"] == ""
    assert out["best"]["combination"] == "a@m" and out["best"]["n"] == 4


def test_thin_evidence_is_flagged_even_with_headroom(tmp_path):
    _write(tmp_path, "demo.json", {"harnesses": {
        "a@m": {"lift": 0.4, "skill_mean": 0.6, "control_mean": 0.2, "tasks_scored": 1,
                "attempts": 3}}})
    out = R.summarize(R.read_matrix("demo", tmp_path)["rows"])
    assert "fewer than 3" in out["warning"] and out["best"] is None


def test_a_single_attempt_matrix_will_not_be_read_as_a_ranking(tmp_path):
    """Two control-arm runs of an identical configuration moved a task by 0.278 and swapped two
    harnesses' rank, while re-judging one fixed answer three times was identical. One attempt per
    task sits under that noise, so the difference between these rows is agent variance."""
    _write(tmp_path, "demo.json", {"harnesses": {
        "a@m": {"lift": 0.25, "skill_mean": 0.60, "control_mean": 0.35, "tasks_scored": 4},
        "b@m": {"lift": 0.05, "skill_mean": 0.40, "control_mean": 0.35, "tasks_scored": 4}}})
    matrix = R.read_matrix("demo", tmp_path)
    out = R.summarize(matrix["rows"])
    assert out["best"] is None
    assert "one attempt" in out["warning"] and "-k 3" in out["warning"]
    assert matrix["rows"][0]["attempts"] == 1


def test_row_level_exploratory_evidence_disables_ranking_without_hiding_numbers(tmp_path):
    _write(tmp_path, "demo.json", {"harnesses": {
        "a@m": {"lift": 0.25, "skill_mean": 0.60, "control_mean": 0.35,
                "tasks_scored": 4, "attempts": 1, "exploratory": True,
                "rankable": False}}})
    matrix = R.read_matrix("demo", tmp_path)

    assert matrix["exploratory"] is True and matrix["rankable"] is False
    assert matrix["mean_lift"] == 0.25 and matrix["rows"][0]["lift"] == 0.25
    assert matrix["model_summaries"]["m"]["best_harness"] is None
    assert "exploratory" in matrix["warning"].lower()


def test_the_rescored_matrix_wins_when_it_is_newer(tmp_path):
    """Rescoring is what removed two fabricated cells from the first grid. Showing the raw file
    over a newer correction would put them back on the page."""
    _write(tmp_path, "demo.json", {"harnesses": {
        "a@m": {"lift": -0.375, "skill_mean": 0.2, "control_mean": 0.575, "tasks_scored": 4}}})
    path = _write(tmp_path, "demo.rescored.json", {"combinations": {
        "a@m": {"lift": 0.125, "skill_mean": 0.7, "control_mean": 0.575, "tasks_scored": 2}}})
    import os
    newer = (tmp_path / "demo.json").stat().st_mtime + 60
    os.utime(path, (newer, newer))
    out = R.read_matrix("demo", tmp_path)
    assert out["rescored"] is True
    assert out["rows"][0]["lift"] == 0.125


def test_the_rescored_schema_recovers_harness_and_model_from_the_key(tmp_path):
    """harbor_rescore writes rows without harness/model fields; the combination key still has them,
    and a matrix that cannot say which model a row used cannot be read as a co-occurrence grid."""
    _write(tmp_path, "demo.rescored.json", {"combinations": {
        "terminus-2@anthropic/claude-opus-5": {
            "lift": 0.1, "skill_mean": 0.6, "control_mean": 0.5, "tasks_scored": 4}}})
    row = R.read_matrix("demo", tmp_path)["rows"][0]
    assert (row["harness"], row["model"]) == ("terminus-2", "anthropic/claude-opus-5")


def test_a_skill_never_run_is_absent_rather_than_empty(tmp_path):
    assert R.read_matrix("never-run", tmp_path) is None


def test_a_corrupt_matrix_raises_rather_than_reading_as_no_results(tmp_path):
    """'No combinations helped' and 'the file is broken' must not look the same on the page."""
    (tmp_path / "demo.json").write_text("{not json")
    with pytest.raises(ValueError, match="unreadable"):
        R.read_matrix("demo", tmp_path)


def test_available_lists_skills_that_have_a_matrix(tmp_path):
    _write(tmp_path, "demo.json", LIVE)
    _write(tmp_path, "other.rescored.json", {"combinations": {}})
    assert sorted(R.available(tmp_path)) == ["demo", "other"]


def _model_matrix_fixture():
    """Five model identities over nine harness identities, with sparse recorded intersections."""
    rows = {
        "claude-code@ceiling-model": {
            "harness": "claude-code", "model": "ceiling-model",
            "target_alias": "dell-qwen", "endpoint_fingerprint": "q" * 64,
            "protocol": "messages", "skill_mean": 0.90, "control_mean": 0.86,
            "lift": 0.04, "tasks_scored": 4, "attempts": 3,
        },
        "codex@ceiling-model": {
            "harness": "codex", "model": "ceiling-model",
            "skill_mean": 0.88, "control_mean": 0.86, "lift": 0.02,
            "tasks_scored": 4, "attempts": 3,
        },
        "aider@thin-model": {
            "harness": "aider", "model": "thin-model",
            "skill_mean": 0.70, "control_mean": 0.40, "lift": 0.30,
            "tasks_scored": 1, "attempts": 3,
        },
        "goose@single-model": {
            "harness": "goose", "model": "single-model",
            "skill_mean": 0.70, "control_mean": 0.40, "lift": 0.30,
            "tasks_scored": 4,
        },
        "opencode@headroom-model": {
            "harness": "opencode", "model": "headroom-model",
            "skill_mean": 0.80, "control_mean": 0.40, "lift": 0.40,
            "tasks_scored": 4, "attempts": 3,
        },
        "pi@headroom-model": {
            "harness": "pi", "model": "headroom-model",
            "skill_mean": 0.60, "control_mean": 0.40, "lift": 0.20,
            "tasks_scored": 4, "attempts": 3,
        },
        "terminus-2@unmeasured-model": {
            "harness": "terminus-2", "model": "unmeasured-model",
            "target_alias": "spark-deepseek", "endpoint_fingerprint": "d" * 64,
            "protocol": "chat", "error": "canary failed",
        },
        "mini-swe-agent@headroom-model": {
            "harness": "mini-swe-agent", "model": "headroom-model",
            "error": "full run failed",
        },
        "openclaw@single-model": {
            "harness": "openclaw", "model": "single-model",
            "skill_mean": 0.65, "control_mean": 0.45, "lift": 0.20,
            "tasks_scored": 4,
        },
    }
    return {"harnesses": rows}


def test_report_preserves_sparse_rows_axes_and_identity_metadata(tmp_path):
    _write(tmp_path, "demo.json", _model_matrix_fixture())

    out = R.read_matrix("demo", tmp_path)

    assert out["models"] == [
        "ceiling-model", "headroom-model", "single-model",
        "thin-model", "unmeasured-model",
    ]
    assert out["harnesses"] == [
        "aider", "claude-code", "codex", "goose", "mini-swe-agent", "openclaw",
        "opencode", "pi", "terminus-2",
    ]
    assert len(out["rows"]) == 9
    row = next(row for row in out["rows"] if row["combination"] == "claude-code@ceiling-model")
    assert row["target_alias"] == "dell-qwen"
    assert row["endpoint_fingerprint"] == "q" * 64
    assert row["protocol"] == "messages"


def test_model_summaries_refuse_ceiling_thin_and_single_attempt_independently(tmp_path):
    _write(tmp_path, "demo.json", _model_matrix_fixture())

    summaries = R.read_matrix("demo", tmp_path)["model_summaries"]

    assert set(summaries) == {
        "ceiling-model", "headroom-model", "single-model",
        "thin-model", "unmeasured-model",
    }
    assert summaries["ceiling-model"]["best_harness"] is None
    assert "ceiling" in summaries["ceiling-model"]["warning"]
    assert summaries["thin-model"]["best_harness"] is None
    assert "fewer than 3" in summaries["thin-model"]["warning"]
    assert summaries["single-model"]["best_harness"] is None
    assert "one attempt" in summaries["single-model"]["warning"]
    assert summaries["headroom-model"]["best_harness"] == "opencode"
    assert summaries["unmeasured-model"] == {
        "model": "unmeasured-model", "measured": 0, "unmeasured": 1,
        "mean_lift": None, "control_mean": None,
        "warning": "No combination produced a measurement.", "best_harness": None,
    }


def test_report_has_no_global_best_and_does_not_synthesize_missing_intersections(tmp_path):
    _write(tmp_path, "demo.json", _model_matrix_fixture())

    out = R.read_matrix("demo", tmp_path)
    failed = next(row for row in out["rows"] if row["combination"] == "terminus-2@unmeasured-model")

    assert "best" not in out
    assert (out["measured"], out["unmeasured"]) == (7, 2)
    assert "lift" not in failed
    assert "skill_mean" not in failed and "control_mean" not in failed and "n" not in failed
    assert failed["target_alias"] == "spark-deepseek"
    assert failed["endpoint_fingerprint"] == "d" * 64 and failed["protocol"] == "chat"
    measured_without_identity = next(
        row for row in out["rows"] if row["combination"] == "codex@ceiling-model"
    )
    assert all(field not in measured_without_identity for field in (
        "target_alias", "endpoint_fingerprint", "protocol"))
    assert len(out["rows"]) < len(out["models"]) * len(out["harnesses"])
