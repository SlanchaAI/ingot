import pytest

from ingot.optimize import harbor_rescore as R


def test_current_compatibility_accepts_only_explicit_k1_exploration():
    holdout = [{"task": "one"}]
    fingerprint = R._task_fingerprint(holdout)
    R._validate_current_compatibility([
        {"task_fingerprint": fingerprint, "attempts": 1,
         "exploratory": True, "rankable": False}
    ], holdout)
    with pytest.raises(ValueError, match="measurement contract"):
        R._validate_current_compatibility([
            {"task_fingerprint": fingerprint, "attempts": 1,
             "exploratory": False, "rankable": True}
        ], holdout)
