"""Lite mode: the Langfuse-free A/B variant runner and the cost ledger / spend cap."""
import pytest

from ingot.optimize import ab as ab_mod
from ingot.optimize import usage as usage_ledger


def _fake_run_task(answers: dict):
    async def run_task(agent, task, config=None, include_behavior=False):
        if isinstance(answers[task], Exception):
            raise answers[task]
        return answers[task], [], {"input_tokens": 10, "output_tokens": 5}, [{"step": "x"}]
    return run_task


def test_local_variant_matches_run_variant_shape(monkeypatch):
    tasks = [{"task": "a", "rubric": "ra"}, {"task": "b", "rubric": "rb"}]
    monkeypatch.setattr(ab_mod, "run_task", _fake_run_task({"a": "ans-a", "b": "ans-b"}))
    monkeypatch.setattr(ab_mod, "judge",
                        lambda t, r, ans, check=None, deliverable=None, checklist=None:
                        {"score": 0.9 if t == "a" else 0.4, "feedback": "f", "dimensions": {}})
    scores, usages, behaviors, answers = ab_mod._run_variant_local(agent=None, tasks=tasks)
    assert scores == [0.9, 0.4]                      # aligned to task order
    assert [u["input_tokens"] for u in usages] == [10, 10]
    assert behaviors == [[{"step": "x"}], [{"step": "x"}]]
    assert answers == ["ans-a", "ans-b"]             # holdout answers for the acceptance gate


def test_local_variant_scores_failed_rollouts_zero(monkeypatch):
    tasks = [{"task": "a", "rubric": "ra"}, {"task": "b", "rubric": "rb"}]
    monkeypatch.setattr(ab_mod, "run_task",
                        _fake_run_task({"a": RuntimeError("provider down"), "b": "ans-b"}))
    monkeypatch.setattr(ab_mod, "judge",
                        lambda t, r, ans, check=None, deliverable=None, checklist=None:
                        {"score": 1.0, "feedback": "f", "dimensions": {}})
    scores, usages, behaviors, answers = ab_mod._run_variant_local(agent=None, tasks=tasks)
    assert scores == [0.0, 1.0]                      # failure defaults to 0, like _run_variant
    assert usages[0] == {"input_tokens": 0, "output_tokens": 0} and behaviors[0] == []
    assert answers == ["", "ans-b"]                  # a failed rollout contributes no answer


def test_estimated_cost_uses_role_model_prices(monkeypatch):
    usage_ledger.reset()
    monkeypatch.delenv("MAX_RUN_USD", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)   # default endpoint: OpenRouter
    monkeypatch.setenv("AGENT_MODEL", "m/agent")
    monkeypatch.setenv("SKILLOPT_MODEL", "m/teacher")
    monkeypatch.setenv("JUDGE_MODEL", "m/judge")
    monkeypatch.setattr(usage_ledger, "_PRICES",
                        {"m/agent": (1e-6, 2e-6), "m/judge": (1e-7, 1e-7)})
    usage_ledger.add("rollout", {"input_tokens": 1_000_000, "output_tokens": 500_000})
    usage_ledger.add("judge", {"input_tokens": 1_000_000, "output_tokens": 0})
    assert usage_ledger.estimated_cost() == pytest.approx(1.0 + 1.0 + 0.1)
    assert "estimated cost: $2.10" in usage_ledger.format_report()
    usage_ledger.reset()


def test_compat_spend_is_priced_per_model_and_reaches_the_cap(monkeypatch):
    """The compatibility sweep changes the serving model on purpose, so it bills to `compat:<model>`
    rather than one bucket. Under a single "compat" role no price ever matched, the sweep counted
    as $0 — it reported $0.04 against $1.42 actually spent — and MAX_RUN_USD, which enforces on the
    same figure, could not see the most expensive role in the run."""
    usage_ledger.reset()
    monkeypatch.delenv("MAX_RUN_USD", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.setenv("JUDGE_MODEL", "m/judge")
    monkeypatch.setattr(usage_ledger, "_PRICES",
                        {"m/dear": (0.0, 1e-5), "m/cheap": (0.0, 1e-7), "m/judge": (0.0, 0.0)})
    usage_ledger.add("compat:m/dear", {"input_tokens": 0, "output_tokens": 100_000})
    usage_ledger.add("compat:m/cheap", {"input_tokens": 0, "output_tokens": 100_000})
    usage_ledger.add("judge", {"input_tokens": 500_000, "output_tokens": 0})

    assert usage_ledger.estimated_cost() == pytest.approx(1.0 + 0.01)
    assert usage_ledger.unpriced_roles() == []
    assert "NOT in that estimate" not in usage_ledger.format_report()
    usage_ledger.reset()


def test_a_role_with_no_price_is_named_instead_of_counting_as_zero(monkeypatch):
    """An unpriced role contributes $0, which is right for a local endpoint and dangerously wrong
    for a slug we simply failed to resolve. Either way the report has to say which roles the
    number excludes, or the next silently-free role hides the same way this one did."""
    usage_ledger.reset()
    monkeypatch.delenv("MAX_RUN_USD", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.setenv("JUDGE_MODEL", "m/judge")
    monkeypatch.setattr(usage_ledger, "_PRICES", {"m/judge": (1e-6, 1e-6)})
    usage_ledger.add("judge", {"input_tokens": 1_000_000, "output_tokens": 0})
    usage_ledger.add("compat:m/unknown", {"input_tokens": 0, "output_tokens": 9_000_000})

    assert usage_ledger.unpriced_roles() == ["compat:m/unknown"]
    report = usage_ledger.format_report()
    assert "NOT in that estimate: compat:m/unknown" in report and "m/unknown" in report
    usage_ledger.reset()


def test_cost_is_none_on_local_endpoints(monkeypatch):
    usage_ledger.reset()
    monkeypatch.setenv("BASE_URL", "http://172.17.0.1:11434/v1")
    usage_ledger.add("rollout", {"input_tokens": 100, "output_tokens": 100})
    assert usage_ledger.estimated_cost() is None
    assert "estimated cost" not in usage_ledger.format_report()
    usage_ledger.reset()


def test_max_run_usd_aborts_past_the_cap(monkeypatch):
    usage_ledger.reset()
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.setenv("AGENT_MODEL", "m/agent")
    monkeypatch.setattr(usage_ledger, "_PRICES", {"m/agent": (1e-5, 1e-5)})
    monkeypatch.setenv("MAX_RUN_USD", "0.5")
    usage_ledger.add("rollout", {"input_tokens": 10_000, "output_tokens": 0})   # $0.10: fine
    with pytest.raises(SystemExit, match="MAX_RUN_USD exceeded"):
        usage_ledger.add("rollout", {"input_tokens": 100_000, "output_tokens": 0})  # $1.10 total
    usage_ledger.reset()
