"""Unit tests for success/failure mining (optimize.mine), Langfuse HTTP and the judge are mocked."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from optimize import mine
from optimize.judge import DIMENSIONS

# Freeze the clock and align the floor with [now+1s-7d, now+1s) so tests that do
# not care about window walking issue exactly one time range.
_NOW = datetime(2026, 11, 13, 17, 0, 0, tzinfo=timezone.utc)
_WINDOW = timedelta(days=7)
_WINDOW_TO = _NOW + timedelta(seconds=1)
_WINDOW_FROM = _WINDOW_TO - _WINDOW


@pytest.fixture(autouse=True)
def _single_fetch_window(monkeypatch):
    monkeypatch.setattr(mine, "_utcnow", lambda: _NOW)
    monkeypatch.setattr(mine, "TRACE_TIME_WINDOW", _WINDOW)
    monkeypatch.setattr(mine, "TRACE_TIME_FLOOR", _WINDOW_FROM)


class _Resp:
    def __init__(self, data):
        self._d = json.dumps({"data": data}).encode()

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_traces_keeps_only_usable_task_output_pairs(monkeypatch):
    data = [
        {"input": {"task": "do X", "rubric": "r"}, "output": "an answer", "tags": ["pdf"]},  # keep
        {"input": {"task": "do Y"}, "output": None, "tags": []},          # null output -> drop
        {"input": {"notask": "z"}, "output": "ans", "tags": []},          # no task -> drop
        {"input": {"task": "do Z"}, "output": "   ", "tags": []},         # blank output -> drop
        {"input": "a bare string", "output": "ans", "tags": []},          # Codex connector -> keep
    ]
    monkeypatch.setattr(mine.urllib.request, "urlopen", lambda req, timeout=60: _Resp(data))
    out = mine.fetch_traces(50)
    assert [(trace["task"], trace["rubric"]) for trace in out] == [
        ("do X", "r"), ("a bare string", "")]


def test_fetch_traces_parses_langgraph_agent_traces(monkeypatch):
    lg = lambda task, answer: {"input": {"messages": [{"role": "user", "content": task}]},
                               "output": {"messages": [{"type": "ai", "content": answer}]}, "tags": ["demo"]}
    data = [
        lg("fill the form", "here is the code"),                                   # keep
        lg("rotate pages", ""),                                                    # empty answer -> drop
        {"input": {"messages": []}, "output": {"messages": []}, "tags": []},       # empty messages -> drop
        {"input": {"messages": [{"content": "blocks"}]},                           # content-block answer -> join
         "output": {"messages": [{"content": [{"type": "text", "text": "part1"},
                                              {"type": "text", "text": "part2"}]}]}, "tags": []},
    ]
    monkeypatch.setattr(mine.urllib.request, "urlopen", lambda req, timeout=60: _Resp(data))
    out = mine.fetch_traces(50)
    assert [t["task"] for t in out] == ["fill the form", "blocks"]
    assert out[0]["answer"] == "here is the code" and out[0]["rubric"] == ""
    assert out[1]["answer"] == "part1\npart2"


@pytest.mark.parametrize(("trace_input", "trace_output", "task", "answer"), [
    ("Codex task", "Codex answer", "Codex task", "Codex answer"),
    ("123", "true", "123", "true"),
    (json.dumps({"role": "user", "content": "Claude task"}),
     json.dumps({"role": "assistant", "content": "Claude answer"}),
     "Claude task", "Claude answer"),
    ({"role": "user", "content": "Claude task"},
     {"role": "assistant", "content": "Claude answer"}, "Claude task", "Claude answer"),
])
def test_fetch_traces_parses_coding_agent_connector_roots(
        monkeypatch, trace_input, trace_output, task, answer):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({"data": [{"input": trace_input, "output": trace_output,
                                         "tags": []}]}).encode()
    monkeypatch.setattr(mine.urllib.request, "urlopen", lambda req, timeout=60: Resp())

    traces = mine.fetch_traces(10)

    assert traces == [{"task": task, "rubric": "", "answer": answer, "tags": []}]


def test_fetch_traces_fails_loudly_when_backend_unreachable(monkeypatch):
    """No local fallback: an unreachable evals backend must raise, not return an empty list that
    would read as 'nothing failing'."""
    def boom(req, timeout=60):
        raise OSError("connection refused")
    monkeypatch.setattr(mine.urllib.request, "urlopen", boom)
    with pytest.raises(SystemExit) as exc:
        mine.fetch_traces(50)
    assert "unreachable" in str(exc.value).lower()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://langfuse.example", "localhost:3100"])
def test_fetch_traces_rejects_non_http_backend_urls_before_opening(monkeypatch, url):
    monkeypatch.setattr(mine, "LF_URL", url)
    opened = []
    monkeypatch.setattr(mine.urllib.request, "urlopen", lambda *args, **kwargs: opened.append(args))

    with pytest.raises(SystemExit, match="absolute http:// or https://"):
        mine.fetch_traces(1)

    assert opened == []


def _assert_observation_query(query, *, from_start, to_start, cursor=None):
    assert query["isRootObservation"] == ["true"]
    assert query["fields"] == ["core,basic,io,trace_context"]
    assert query["limit"] == ["100"]
    assert "page" not in query
    assert query["fromStartTime"] == [from_start]
    assert query["toStartTime"] == [to_start]
    # fromStartTime is inclusive, toStartTime is exclusive.
    assert from_start < to_start
    if cursor is None:
        assert "cursor" not in query
    else:
        assert query["cursor"] == [cursor]


def test_fetch_traces_zero_limit_follows_cursors_and_preserves_shape(monkeypatch):
    first = {"id": "root-one", "traceId": "trace-one",
             "input": json.dumps({"task": "task one", "rubric": "r"}),
             "output": json.dumps("answer one"), "tags": ["pdf"]}
    pages = [
        {"data": [first], "meta": {"cursor": "next+/="}},
        # Duplicate-only short pages must not terminate cursor traversal.
        {"data": [first], "meta": {"cursor": "last"}},
        {"data": [{"id": "root-two", "traceId": "trace-two",
                   "input": "task two", "output": "answer two"}],
         "meta": {"cursor": None}},
    ]
    requested = []
    from_start = mine._iso8601(_WINDOW_FROM)
    to_start = mine._iso8601(_WINDOW_TO)

    def urlopen(request, timeout=60):
        url = urlparse(request.full_url)
        query = parse_qs(url.query)
        assert url.path == "/api/public/v2/observations"
        cursor = query.get("cursor", [None])[0]
        _assert_observation_query(query, from_start=from_start, to_start=to_start,
                                  cursor=cursor)
        requested.append(query.get("cursor"))
        response = _Resp([])
        response._d = json.dumps(pages[len(requested) - 1]).encode()
        return response

    monkeypatch.setattr(mine.urllib.request, "urlopen", urlopen)

    assert mine.fetch_traces(0) == [
        {"task": "task one", "rubric": "r", "answer": "answer one", "tags": ["pdf"]},
        {"task": "task two", "rubric": "", "answer": "answer two", "tags": []},
    ]
    assert requested == [None, ["next+/="], ["last"]]


def test_fetch_traces_walks_adjacent_time_windows(monkeypatch):
    """Newest-first 7-day windows share an exclusive boundary and page independently."""
    now = datetime(2026, 11, 13, 17, 0, 0, tzinfo=timezone.utc)
    window = timedelta(days=7)
    newer_to = now + timedelta(seconds=1)
    newer_from = newer_to - window
    older_to = newer_from
    older_from = newer_to - timedelta(days=14)
    monkeypatch.setattr(mine, "_utcnow", lambda: now)
    monkeypatch.setattr(mine, "TRACE_TIME_WINDOW", window)
    monkeypatch.setattr(mine, "TRACE_TIME_FLOOR", older_from)
    # Boundary is exclusive on the newer window's lower side? from is inclusive,
    # to is exclusive, and the next window's to equals the previous from: no overlap.
    assert older_to == newer_from

    pages = {
        (mine._iso8601(newer_from), mine._iso8601(newer_to), None): {
            "data": [{"id": "r1", "traceId": "t-new",
                      "input": "new task", "output": "new answer", "tags": ["new"]}],
            "meta": {"cursor": "newer-next"},
        },
        (mine._iso8601(newer_from), mine._iso8601(newer_to), "newer-next"): {
            "data": [{"id": "r2", "traceId": "t-new-2",
                      "input": "new task 2", "output": "new answer 2", "tags": []}],
            "meta": {},
        },
        (mine._iso8601(older_from), mine._iso8601(older_to), None): {
            "data": [{"id": "r3", "traceId": "t-old",
                      "input": json.dumps({"task": "old task", "rubric": "old-r"}),
                      "output": "old answer", "tags": ["old"]}],
            "meta": {},
        },
    }
    requested = []

    def urlopen(request, timeout=60):
        url = urlparse(request.full_url)
        query = parse_qs(url.query)
        assert url.path == "/api/public/v2/observations"
        from_start = query["fromStartTime"][0]
        to_start = query["toStartTime"][0]
        cursor = query.get("cursor", [None])[0]
        _assert_observation_query(query, from_start=from_start, to_start=to_start,
                                  cursor=cursor)
        key = (from_start, to_start, cursor)
        requested.append(key)
        response = _Resp([])
        response._d = json.dumps(pages[key]).encode()
        return response

    monkeypatch.setattr(mine.urllib.request, "urlopen", urlopen)

    assert mine.fetch_traces(0) == [
        {"task": "new task", "rubric": "", "answer": "new answer", "tags": ["new"]},
        {"task": "new task 2", "rubric": "", "answer": "new answer 2", "tags": []},
        {"task": "old task", "rubric": "old-r", "answer": "old answer", "tags": ["old"]},
    ]
    assert requested == [
        (mine._iso8601(newer_from), mine._iso8601(newer_to), None),
        (mine._iso8601(newer_from), mine._iso8601(newer_to), "newer-next"),
        (mine._iso8601(older_from), mine._iso8601(older_to), None),
    ]
    # Windows are adjacent: older.to == newer.from, so an observation at the
    # boundary is only eligible for the older window (toStartTime is exclusive).
    assert requested[0][0] == requested[2][1]


def test_fetch_traces_empty_result(monkeypatch):
    monkeypatch.setattr(mine.urllib.request, "urlopen", lambda req, timeout=60: _Resp([]))
    assert mine.fetch_traces() == []


def test_fetch_traces_keeps_page_size_fixed_when_output_limit_stops_mid_page(monkeypatch):
    requested = []

    def urlopen(request, timeout=60):
        query = parse_qs(urlparse(request.full_url).query)
        requested.append((query.get("cursor"), int(query["limit"][0])))
        start = (len(requested) - 1) * 100
        data = [{"id": str(i), "input": f"task {i}", "output": f"answer {i}"}
                for i in range(start, start + 100)]
        response = _Resp([])
        response._d = json.dumps({"data": data, "meta": {"cursor": "next"}}).encode()
        return response

    monkeypatch.setattr(mine.urllib.request, "urlopen", urlopen)
    traces = mine.fetch_traces(150)
    assert requested == [(None, 100), (["next"], 100)]
    assert len(traces) == 150


def test_relevant_traces_keeps_tagged_or_embedding_ranked(monkeypatch):
    class FakeRouter:
        def __init__(self, skills):
            pass

        def suggest(self, task, k=5, min_score=0.0):
            return [{"name": "excel"}] if "spreadsheet" in task else [{"name": "other"}]

    monkeypatch.setattr("mcp_server.router.Router", FakeRouter)
    monkeypatch.setattr("mcp_server.registry.load_skills", lambda: [])
    traces = [
        {"task": "spreadsheet lookup with discounts", "tags": []},   # ranked -> keep (misrouted traffic)
        {"task": "rotate a pdf", "tags": ["excel"]},                 # tagged -> keep
        {"task": "rotate a pdf", "tags": ["pdf"]},                   # neither -> drop
    ]
    assert mine.relevant_traces(traces, "excel") == traces[:2]


def test_mine_exits_when_no_traces_are_relevant(monkeypatch):
    import pytest
    monkeypatch.setattr(mine, "fetch_traces",
                        lambda limit: [{"task": "t", "rubric": "", "answer": "a", "tags": []}])
    monkeypatch.setattr(mine, "relevant_traces", lambda traces, skill, k=5: [])
    with pytest.raises(SystemExit) as exc:
        mine.mine("pdf", log=lambda *a, **k: None)
    assert "relevant to 'pdf'" in str(exc.value)


def test_mine_aggregates_failure_dimensions_and_mines_weakest(monkeypatch, tmp_path):
    traces = [{"task": f"t{i}", "rubric": "r", "answer": "a", "tags": []} for i in range(3)]
    monkeypatch.setattr(mine, "fetch_traces", lambda limit: traces)
    monkeypatch.setattr(mine, "relevant_traces", lambda t, skill, k=5: t)
    monkeypatch.setattr(mine, "JUDGE_CACHE_FILE", tmp_path / "judgments.json")
    monkeypatch.setattr(mine, "_cluster_traces",
                        lambda traces, norm_embed: [[i] for i in range(len(traces))])
    verdicts = iter([
        {"score": 0.1, "feedback": "f", "dimensions": {**{d: "pass" for d in DIMENSIONS}, "correctness": "wrong API"}},
        {"score": 0.2, "feedback": "f", "dimensions": {**{d: "pass" for d in DIMENSIONS}, "correctness": "also wrong"}},
        {"score": 0.9, "feedback": "f", "dimensions": {d: "pass" for d in DIMENSIONS}},
    ])
    monkeypatch.setattr(mine, "judge", lambda task, rubric, answer: next(verdicts))
    monkeypatch.setattr(mine, "_normalized_embedder", lambda: None)
    monkeypatch.setattr(mine, "_select_candidates", lambda *args, **kwargs: [0, 1])
    r = mine.mine("pdf", log=lambda *a, **k: None)
    assert r["traces"] == 3
    assert r["failure_dimensions"].get("correctness") == 2          # two traces failed correctness
    assert abs(r["mean_score"] - (0.1 + 0.2 + 0.9) / 3) < 1e-9
    assert r["mined_tasks"][0]["task"] in {"t0", "t1"}              # weakest first
    assert r["judge_calls"] == 3 and r["coverage"] == 1.0


def test_trace_clusters_collapse_exact_and_semantic_paraphrases():
    import numpy as np
    traces = [
        {"task": "Merge PDF files"},
        {"task": " merge   pdf FILES "},
        {"task": "Combine two PDFs"},
        {"task": "Calculate a spreadsheet formula"},
    ]
    vectors = {
        "Merge PDF files": [1.0, 0.0],
        "Combine two PDFs": [0.95, 0.3122499],
        "Calculate a spreadsheet formula": [0.0, 1.0],
    }

    clusters = mine._cluster_traces(
        traces, lambda texts: np.asarray([vectors[text] for text in texts], dtype=np.float32),
        threshold=0.90)

    assert clusters == [[0, 1, 2], [3]]


def test_trace_clusters_keep_different_rubrics_separate():
    import numpy as np
    traces = [
        {"task": "same task", "rubric": "must return JSON"},
        {"task": "same task", "rubric": "must return YAML"},
    ]

    clusters = mine._cluster_traces(
        traces, lambda texts: np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float32))

    assert clusters == [[0], [1]]


def test_cluster_judgments_are_cached_and_frequency_weighted(monkeypatch, tmp_path):
    traces = [
        {"task": "same", "rubric": "", "answer": "a"},
        {"task": "same paraphrase", "rubric": "", "answer": "b"},
        {"task": "different", "rubric": "", "answer": "c"},
    ]
    clusters = [[0, 1], [2]]
    calls = []

    def fake_judge(task, rubric, answer):
        calls.append(task)
        return {"score": 0.25 if task == "same" else 0.75, "feedback": "f",
                "dimensions": {d: "pass" for d in DIMENSIONS}}

    monkeypatch.setattr(mine, "judge", fake_judge)
    cache = tmp_path / "judgments.json"

    first, backlog, first_calls = mine._judge_trace_clusters(traces, clusters, log=lambda *_: None,
                                                              cache_path=cache)
    second, second_backlog, second_calls = mine._judge_trace_clusters(
        traces, clusters, log=lambda *_: None, cache_path=cache)

    assert [(verdict["score"], weight) for _, verdict, weight in first] == [(0.25, 2), (0.75, 1)]
    assert second == first
    assert backlog == second_backlog == 0
    assert first_calls == 2 and second_calls == 0
    assert calls == ["same", "different"]


def test_judge_budget_drains_uncached_clusters_across_runs(monkeypatch, tmp_path):
    traces = [{"task": f"t{i}", "rubric": "", "answer": "a"} for i in range(3)]
    clusters = [[0], [1], [2]]
    monkeypatch.setattr(mine, "MAX_JUDGE_CALLS", 1)
    monkeypatch.setattr(mine, "judge", lambda *args: {
        "score": 0.5, "feedback": "f", "dimensions": {d: "pass" for d in DIMENSIONS}})
    cache = tmp_path / "judgments.json"

    coverages = []
    for _ in range(3):
        scored, backlog, calls = mine._judge_trace_clusters(
            traces, clusters, log=lambda *_: None, cache_path=cache)
        coverages.append((len(scored), backlog, calls))

    assert coverages == [(1, 2, 1), (2, 1, 1), (3, 0, 1)]


def test_judge_cache_invalidates_when_answer_or_judge_changes(monkeypatch, tmp_path):
    traces = [{"task": "task", "rubric": "rubric", "answer": "first"}]
    cache = tmp_path / "judgments.json"
    calls = []
    monkeypatch.setattr(mine, "judge", lambda *args: calls.append(args) or {
        "score": 1.0, "feedback": "f", "dimensions": {d: "pass" for d in DIMENSIONS}})
    monkeypatch.setattr(mine, "MODELS", ["judge/one"])

    mine._judge_trace_clusters(traces, [[0]], log=lambda *_: None, cache_path=cache)
    mine._judge_trace_clusters(traces, [[0]], log=lambda *_: None, cache_path=cache)
    traces[0]["answer"] = "changed"
    mine._judge_trace_clusters(traces, [[0]], log=lambda *_: None, cache_path=cache)
    monkeypatch.setattr(mine, "MODELS", ["judge/two"])
    mine._judge_trace_clusters(traces, [[0]], log=lambda *_: None, cache_path=cache)

    assert len(calls) == 3


def test_corrupt_judge_cache_is_rebuilt(monkeypatch, tmp_path):
    cache = tmp_path / "judgments.json"
    cache.write_text("not json")
    trace = {"task": "task", "rubric": "", "answer": "answer"}
    monkeypatch.setattr(mine, "judge", lambda *args: {
        "score": 0.5, "feedback": "f", "dimensions": {d: "pass" for d in DIMENSIONS}})

    scored, backlog, calls = mine._judge_trace_clusters(
        [trace], [[0]], log=lambda *_: None, cache_path=cache)

    assert len(scored) == 1 and backlog == 0 and calls == 1
    assert json.loads(cache.read_text())["version"] == mine.CACHE_VERSION


def test_mine_weights_every_use_by_cluster_frequency(monkeypatch, tmp_path):
    traces = [
        {"task": "frequent failure", "rubric": "r", "answer": "bad", "tags": []},
        {"task": "frequent failure", "rubric": "r", "answer": "bad again", "tags": []},
        {"task": "rare success", "rubric": "r", "answer": "good", "tags": []},
    ]
    monkeypatch.setattr(mine, "fetch_traces", lambda limit: traces)
    monkeypatch.setattr(mine, "relevant_traces", lambda values, skill, k=5: values)
    monkeypatch.setattr(mine, "JUDGE_CACHE_FILE", tmp_path / "judgments.json")
    monkeypatch.setattr(mine, "_cluster_traces", lambda values, embed: [[0, 1], [2]])
    monkeypatch.setattr(mine, "_select_candidates", lambda *args, **kwargs: [0])
    monkeypatch.setattr(mine, "judge", lambda task, *args: {
        "score": 0.0 if task == "frequent failure" else 1.0,
        "feedback": "f",
        "dimensions": {**{d: "pass" for d in DIMENSIONS},
                       "correctness": "wrong" if task == "frequent failure" else "pass"},
    })

    monkeypatch.setattr(mine, "_normalized_embedder", lambda: None)
    result = mine.mine("pdf", log=lambda *_: None)

    assert result["traces"] == 3 and result["clusters"] == 2
    assert result["mean_score"] == pytest.approx(1 / 3)
    assert result["failure_dimensions"]["correctness"] == 2
    assert result["mined_tasks"][0]["occurrences"] == 2
