# carn panels — read-only evidence UI

Renders carn's on-disk artifacts next to the approval UI: the measured replay story,
compiled replay graphs, and a live trajectory trie with pass/fail forks. The fork view is
the behavioral counterpart to the champion-vs-challenger diff: it shows *where* runs
diverge, not just what changed in the text.

## Gating (launch safety)

Everything hangs on the `CARN_DIR` env var (path to a local carn checkout containing
`scripts/trie_forks.py`). Unset or invalid:

- every `/api/carn/*` endpoint and `/carn` return 404
- `/api/config` reports `carn_enabled: false` and the index link stays hidden
- no import of carn code happens

Functional behavior without `CARN_DIR` is unchanged. Two visible-but-inert deltas:
the carn routes are listed in `/openapi.json` and `/docs`, and their 404 body names
the feature ("carn panels disabled"). Neither affects the approval flow.

Trust boundary: `CARN_DIR` causes `scripts/trie_forks.py` from that checkout to be
imported in-process, and `/api/carn/overview` echoes the checkout's absolute path.
`CARN_DIR` is operator configuration for a local/trusted deployment, never request
input, and should stay unset on any public box.

## Surface

| Route | Source | Shows |
|---|---|---|
| `GET /carn` | `ui/static/carn.html` | the page |
| `GET /api/carn/overview` | `demo_replay/result.json`, `rescue_result.json` | hot-path comparison, rescue ladder, honest-claim fields verbatim |
| `GET /api/carn/graphs` | `demo_replay/*_graph.json` | DAG nodes: action + checkpoint |
| `GET /api/carn/runs` | `CARN_DIR/runs/**/trajectory.json` + `test_output.txt` | run list, pytest-derived pass/fail |
| `GET /api/carn/trie?runs=a,b` | computed live by carn's `trie_forks.py` (imported by path) | trie, outcome forks, token chains |

With no labeled runs on disk the trie endpoint serves an illustrative exemplar flagged
`demo: true` (nine synthetic weak-agent trajectories across three task families —
crack-7z, fix-git, a shell-script task — tuned to surface three outcome forks), and the
page labels it as demo data. Runs that can't carry a signal (no test output, unreadable
trajectory) are reported in `skipped`, shown on the page, and selectable-but-disabled in
the run picker.

Read-only by construction: no endpoint launches runs, walks graphs, or writes.
The `runs` query param is resolved and rejected if it escapes `CARN_DIR/runs/`;
names are used verbatim (no `:pass`/`:fail` label overrides — labels come only from
each run's own test output).

## Verified (2026-07-14, local)

- `CARN_DIR` set: full `ui.app` boots, `/carn` 200, all four endpoints return real data,
  page drives in Chromium (light + dark, run-trace pin, raw-command toggle), no console
  errors beyond the pre-existing favicon 404.
- `CARN_DIR` unset: `/carn` and `/api/carn/*` 404, index link hidden, approval flow untouched.

## Non-goals (v1)

Triggering walks or runs from the browser; Langfuse-backed A/B trajectory forks (needs
per-task trajectory persistence in `optimize/ab.py` — the natural v2 once that exists).
