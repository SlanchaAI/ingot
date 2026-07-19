"""Unit tests for the per-run token ledger (optimize.usage), including thread-safety."""
import threading

from optimize import usage


def test_add_accumulates_per_role_and_totals():
    usage.reset()
    usage.add("judge", {"input_tokens": 10, "output_tokens": 4})
    usage.add("judge", {"input_tokens": 6, "output_tokens": 2})
    usage.add("rollout", {"input_tokens": 100, "output_tokens": 50})
    r = usage.report()
    assert r["judge"] == {"input": 16, "output": 6, "calls": 2}
    assert r["total"] == {"input": 116, "output": 56, "calls": 3}   # 6 (judge) + 50 (rollout)


def test_add_ignores_none_usage():
    usage.reset()
    usage.add("judge", None)
    usage.add("judge", {})
    assert usage.report()["total"]["calls"] == 0


def test_reset_clears_the_ledger():
    usage.reset()
    usage.add("x", {"input_tokens": 1, "output_tokens": 1})
    usage.reset()
    assert usage.report()["total"] == {"input": 0, "output": 0, "calls": 0}


def test_concurrent_adds_are_thread_safe():
    # the candidate search fans rollout+judge across a thread pool, so the lock has to prevent
    # lost increments
    usage.reset()

    def worker():
        for _ in range(1000):
            usage.add("rollout", {"input_tokens": 1, "output_tokens": 1})

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    r = usage.report()["rollout"]
    assert r["calls"] == 8000 and r["input"] == 8000 and r["output"] == 8000


def test_format_report_is_readable():
    usage.reset()
    usage.add("judge", {"input_tokens": 1234, "output_tokens": 56})
    out = usage.format_report()
    assert "judge" in out and "TOTAL" in out and "1,234" in out
