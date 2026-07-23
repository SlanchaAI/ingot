from pathlib import Path

import numpy as np

from experiments.skill_decomposer_matcher import (
    Section,
    Match,
    MatchConfig,
    analyze_corpus,
    build_clusters,
    decompose_skill,
    match_sections,
    render_markdown,
)


def test_decompose_skill_tracks_heading_path_and_ignores_fenced_headings(tmp_path: Path):
    source = tmp_path / "example" / "SKILL.md"
    source.parent.mkdir()
    source.write_text(
        """---
name: example
description: Example skill.
---

# Example

Opening text that is long enough to retain as a section for this test.

## Verify

Run the focused test, inspect the result, and stop when the evidence is clean.

```markdown
# This is code, not a section
```

### Failure path

Record the failing command and diagnose the cause before another attempt.
""",
        encoding="utf-8",
    )

    sections = decompose_skill(source, min_chars=20)

    assert [section.heading for section in sections] == [
        "Example",
        "Example > Verify",
        "Example > Verify > Failure path",
    ]
    assert "This is code, not a section" in sections[1].body


def test_match_sections_returns_only_cross_skill_neighbors():
    sections = [
        Section("a", "Verify", "a", Path("a/SKILL.md"), 1),
        Section("a", "Stop", "b", Path("a/SKILL.md"), 2),
        Section("b", "Verification", "c", Path("b/SKILL.md"), 1),
    ]
    vectors = np.array([
        [1.0, 0.0],
        [0.99, 0.01],
        [1.0, 0.0],
    ])

    matches = match_sections(sections, vectors, neighbors=2, min_score=0.5)

    assert [(match.left.skill, match.right.skill) for match in matches] == [
        ("a", "b"),
        ("a", "b"),
    ]
    assert matches[0].score == 1.0


def test_build_clusters_requires_three_distinct_skills():
    sections = [
        Section("a", "Verify", "a", Path("a/SKILL.md"), 1),
        Section("b", "Verify", "b", Path("b/SKILL.md"), 1),
        Section("c", "Verify", "c", Path("c/SKILL.md"), 1),
        Section("x", "Setup", "x", Path("x/SKILL.md"), 1),
        Section("y", "Setup", "y", Path("y/SKILL.md"), 1),
    ]
    vectors = np.array([
        [1.0, 0.0],
        [0.99, 0.01],
        [0.98, 0.02],
        [0.0, 1.0],
        [0.01, 0.99],
    ])
    matches = match_sections(sections, vectors, neighbors=2, min_score=0.9)

    clusters = build_clusters(sections, matches, min_skills=3)

    assert len(clusters) == 1
    assert {section.skill for section in clusters[0].sections} == {"a", "b", "c"}


def test_build_clusters_rejects_similarity_chains_without_all_pair_matches():
    alpha = Section("alpha", "Verify", "a", Path("alpha/SKILL.md"), 1)
    beta = Section("beta", "Verify", "b", Path("beta/SKILL.md"), 1)
    gamma = Section("gamma", "Verify", "c", Path("gamma/SKILL.md"), 1)
    matches = [Match(alpha, beta, 0.9), Match(beta, gamma, 0.9)]

    clusters = build_clusters([alpha, beta, gamma], matches, min_skills=3)

    assert clusters == []


class _SameVectorEmbedder:
    def embed(self, texts):
        return [np.array([1.0, 0.0]) for _ in texts]


def test_analyze_corpus_produces_a_reproducible_cluster_report(tmp_path: Path):
    for skill in ("alpha", "beta", "gamma"):
        skill_dir = tmp_path / skill
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"""---
name: {skill}
description: {skill} skill.
---

# Review and verify

Inspect the proposed change, run focused checks, and record the evidence before approval.
""",
            encoding="utf-8",
        )

    analysis = analyze_corpus(
        tmp_path,
        _SameVectorEmbedder(),
        MatchConfig(min_chars=20, neighbors=2, min_score=0.9, min_skills=3),
    )
    report = render_markdown(analysis, tmp_path, top_matches=5)

    assert analysis.skill_count == 3
    assert len(analysis.sections) == 3
    assert len(analysis.clusters) == 1
    assert "3 skills, 3 sections" in report
    assert "alpha: Review and verify" in report
