# Body-aware routing design

## Goal

Improve Ingot's skill selection when decisive workflow terms appear in the
approved skill body rather than its short description, without weakening
compatibility filtering, collision detection, score thresholds, or the
one-body serving contract.

## Evidence

The SkillRouter evaluation embeds skill name, description, and body, then
reranks retrieved candidates with full content. Its reported body ablation
loses 29–44 top-1 points across its settings. Ingot currently embeds only the
description. [P, SkillRouter repository and paper, 96]

The broader competitor atlas favors coarse-to-fine and hybrid retrieval, but a
new reranker model would add weight, latency, and licensing questions before a
local baseline proves value. The first slice therefore reuses Ingot's pinned
embedding backend and adds no dependency. [S, architecture choice, 92]

## Serving contract

`Router` keeps two representations per approved skill:

- **description**: the existing description-only document. It remains the sole
  representation for `nearest`, so description collision checks do not change.
- **content**: a bounded document containing name, description, and the
  harness-specific approved body.

A task query is embedded once. Each compatible skill receives:

```text
description_score = cosine(query, description)
content_score = cosine(query, content)
score = max(description_score, content_score)
matched_on = "description" if description_score >= content_score else "content"
```

The max preserves every prior description score while allowing body evidence
to lift a candidate. Existing `MIN_SCORE` and `RELATED_SCORE` therefore retain
their description-side meaning; deployment still needs a larger held-out
calibration before treating body-triggered thresholds as final.

Compatibility filtering runs before ranking. Priority and name remain
deterministic tie-breakers. Conflicts still filter the returned ranking.

## Bounded content

The content document includes at most `ROUTER_BODY_CHARS` body characters,
default 16,000. The value must be a positive integer. Name and description are
always retained. Harness variants receive separate cached representations.

The cache key includes embedding model, representation kind, and exact text.
A body or variant change therefore misses the content cache while unchanged
descriptions continue to reuse their vectors.

## Response changes

Selected matches and alternatives gain additive metadata:

```json
{
  "score_components": {"description": 0.412, "content": 0.731},
  "matched_on": "content"
}
```

No existing field changes meaning. Alternatives remain body-free.
`route_and_load` remains the live caller through the Model Context Protocol
(MCP) server and bundled agent.

## Failure behavior

- Empty libraries avoid embedding initialization and return no match.
- Incompatible content cannot affect ranking.
- Body text never crosses the boundary unless that skill is selected.
- A content vector cannot change `nearest` collision behavior.
- Invalid or zero `ROUTER_BODY_CHARS` fails at router construction instead of
  silently truncating to an unintended document.

## Success criteria

1. A deterministic regression fixture with identical descriptions routes by
   the decisive approved body.
2. Compatibility filtering prevents an incompatible body from winning.
3. Harness variants produce distinct content routes.
4. Description vector reuse remains intact across refreshes.
5. Response explanations identify the winning component without exposing
   alternative bodies.
6. Existing focused and full suites pass in the repository runtime.
7. The committed routing suite has no regression. A larger held-out corpus is
   required before claiming a production accuracy lift.

## Deferred

- Lexical retrieval, learned reranking, graph expansion, and ambiguity
  clarification.
- CARN replay admission and compatibility manifests.
- Automatic threshold changes.

Each deferred mechanism needs its own baseline, caller, and evidence gate.
