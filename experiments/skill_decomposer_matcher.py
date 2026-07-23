"""Decompose SKILL.md files and find similar workflow sections across skills."""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class Section:
    skill: str
    heading: str
    body: str
    source: Path
    line: int


@dataclass(frozen=True)
class Match:
    left: Section
    right: Section
    score: float


@dataclass(frozen=True)
class Cluster:
    sections: tuple[Section, ...]
    mean_score: float


@dataclass(frozen=True)
class Analysis:
    skill_count: int
    sections: tuple[Section, ...]
    matches: tuple[Match, ...]
    clusters: tuple[Cluster, ...]


@dataclass(frozen=True)
class MatchConfig:
    min_chars: int = 120
    neighbors: int = 3
    min_score: float = 0.78
    min_skills: int = 3


def _without_frontmatter(lines: list[str]) -> tuple[list[str], int]:
    if not lines or lines[0].strip() != "---":
        return lines, 1
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return lines[index + 1 :], index + 2
    return lines, 1


def _heading(line: str) -> tuple[int, str] | None:
    stripped = line.lstrip()
    hashes = len(stripped) - len(stripped.lstrip("#"))
    if not 1 <= hashes <= 6:
        return None
    if len(stripped) == hashes:
        return None
    if stripped[hashes] != " ":
        return None
    title = stripped[hashes + 1 :].strip().rstrip("#").rstrip()
    return (hashes, title) if title else None


def decompose_skill(source: Path, min_chars: int = 80) -> list[Section]:
    raw_lines = source.read_text(encoding="utf-8", errors="ignore").splitlines()
    lines, first_line = _without_frontmatter(raw_lines)
    skill = source.parent.name
    sections: list[Section] = []
    heading_path: list[str] = []
    active_heading = ""
    active_line = first_line
    body: list[str] = []
    fence: str | None = None

    def flush() -> None:
        text = "\n".join(body).strip()
        if active_heading and len(text) >= min_chars:
            sections.append(Section(skill, active_heading, text, source, active_line))

    for offset, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
            body.append(line)
            continue
        parsed = None if fence else _heading(line)
        if not parsed:
            body.append(line)
            continue
        flush()
        level, title = parsed
        heading_path[level - 1 :] = []
        while len(heading_path) < level - 1:
            heading_path.append("(untitled)")
        heading_path.append(title)
        active_heading = " > ".join(heading_path)
        active_line = first_line + offset
        body = []
    flush()
    return sections


def match_sections(
    sections: list[Section],
    vectors: np.ndarray,
    *,
    neighbors: int,
    min_score: float,
) -> list[Match]:
    if len(sections) != len(vectors):
        raise ValueError("one vector is required for every section")
    if not sections:
        return []
    matrix = np.asarray(vectors, dtype=np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-8
    scores = matrix @ matrix.T
    pairs: dict[tuple[int, int], Match] = {}
    for left_index, left in enumerate(sections):
        candidates = [
            (float(scores[left_index, right_index]), right_index)
            for right_index, right in enumerate(sections)
            if right.skill != left.skill and scores[left_index, right_index] >= min_score
        ]
        for score, right_index in sorted(candidates, key=lambda item: (-item[0], item[1]))[:neighbors]:
            pair = tuple(sorted((left_index, right_index)))
            pairs[pair] = Match(sections[pair[0]], sections[pair[1]], round(score, 6))
    return sorted(
        pairs.values(),
        key=lambda match: (-match.score, match.left.skill, match.left.heading,
                           match.right.skill, match.right.heading),
    )


def build_clusters(
    sections: list[Section],
    matches: list[Match],
    *,
    min_skills: int,
) -> list[Cluster]:
    neighbors, score_by_edge = _match_graph(sections, matches)
    cliques = _maximal_cliques(neighbors, min_skills)
    clusters = [_cluster(clique, sections, score_by_edge) for clique in cliques]
    return sorted(
        clusters,
        key=lambda cluster: (-len(cluster.sections), -cluster.mean_score,
                             cluster.sections[0].skill),
    )


def _match_graph(
    sections: list[Section],
    matches: list[Match],
) -> tuple[dict[int, set[int]], dict[tuple[int, int], float]]:
    index_by_section = {section: index for index, section in enumerate(sections)}
    neighbors = {index: set() for index in range(len(sections))}
    score_by_edge: dict[tuple[int, int], float] = {}
    for match in matches:
        left = index_by_section[match.left]
        right = index_by_section[match.right]
        neighbors[left].add(right)
        neighbors[right].add(left)
        score_by_edge[tuple(sorted((left, right)))] = match.score
    return neighbors, score_by_edge


def _maximal_cliques(neighbors: dict[int, set[int]], min_size: int) -> list[set[int]]:
    cliques: list[set[int]] = []

    def visit(chosen: set[int], candidates: set[int], excluded: set[int]) -> None:
        if not candidates and not excluded:
            if len(chosen) >= min_size:
                cliques.append(set(chosen))
            return
        pool = candidates | excluded
        pivot = max(pool, key=lambda node: len(candidates & neighbors[node])) if pool else None
        expandable = candidates - (neighbors[pivot] if pivot is not None else set())
        for node in sorted(expandable):
            visit(
                chosen | {node},
                candidates & neighbors[node],
                excluded & neighbors[node],
            )
            candidates.remove(node)
            excluded.add(node)

    visit(set(), set(neighbors), set())
    return cliques


def _cluster(
    clique: set[int],
    sections: list[Section],
    score_by_edge: dict[tuple[int, int], float],
) -> Cluster:
    ordered_indices = sorted(clique)
    edge_scores = [score_by_edge[pair] for pair in combinations(ordered_indices, 2)]
    selected = (sections[index] for index in ordered_indices)
    ordered = tuple(sorted(selected, key=lambda section: (section.skill, section.heading)))
    return Cluster(ordered, round(float(np.mean(edge_scores)), 6))


def _skill_sources(root: Path) -> list[Path]:
    return sorted(
        source for source in root.rglob("SKILL.md")
        if "node_modules" not in source.parts
        and not any(part.startswith(".") for part in source.relative_to(root).parts)
    )


def analyze_corpus(
    root: Path,
    embedder,
    config: MatchConfig,
) -> Analysis:
    sources = _skill_sources(root)
    sections = [
        section
        for source in sources
        for section in decompose_skill(source, min_chars=config.min_chars)
    ]
    if not sections:
        return Analysis(len(sources), (), (), ())
    texts = [f"{section.heading}\n\n{section.body}" for section in sections]
    vectors = np.asarray(list(embedder.embed(texts)), dtype=np.float32)
    matches = match_sections(
        sections,
        vectors,
        neighbors=config.neighbors,
        min_score=config.min_score,
    )
    clusters = build_clusters(sections, matches, min_skills=config.min_skills)
    return Analysis(len(sources), tuple(sections), tuple(matches), tuple(clusters))


def render_markdown(analysis: Analysis, root: Path, *, top_matches: int) -> str:
    lines = [
        "# Skill decomposer and matcher candidates",
        "",
        f"Corpus: `{root}`",
        "",
        (f"Parsed {analysis.skill_count} skills, {len(analysis.sections)} sections, "
         f"{len(analysis.matches)} cross-skill matches, and {len(analysis.clusters)} clusters."),
        "",
        "Candidate clusters require sections from at least the configured number of distinct skills. "
        "They are retrieval leads, not proof that a shared workflow exists.",
        "",
        "## Candidate clusters",
    ]
    lines.extend(_cluster_lines(analysis.clusters, root))
    lines.extend(["", "## Strongest section pairs", "", *_match_lines(analysis, top_matches)])
    return "\n".join(lines).rstrip() + "\n"


def _cluster_lines(clusters: tuple[Cluster, ...], root: Path) -> list[str]:
    if not clusters:
        return ["", "No clusters passed the configured threshold."]
    return [
        line
        for index, cluster in enumerate(clusters, start=1)
        for line in _one_cluster_lines(index, cluster, root)
    ]


def _one_cluster_lines(index: int, cluster: Cluster, root: Path) -> list[str]:
    skill_count = len({section.skill for section in cluster.sections})
    heading = f"### Cluster {index}: {skill_count} skills, mean matched cosine {cluster.mean_score:.3f}"
    return ["", heading, "", *(_section_line(section, root) for section in cluster.sections)]


def _section_line(section: Section, root: Path) -> str:
    relative = _relative_source(section.source, root)
    snippet = " ".join(section.body.split())[:240]
    return f"- **{section.skill}: {section.heading}** (`{relative}:{section.line}`): {snippet}"


def _match_lines(analysis: Analysis, top_matches: int) -> list[str]:
    return [
        f"- `{match.score:.3f}` **{match.left.skill}: {match.left.heading}** ↔ "
        f"**{match.right.skill}: {match.right.heading}**"
        for match in analysis.matches[:top_matches]
    ]


def _relative_source(source: Path, root: Path) -> Path:
    try:
        return source.relative_to(root)
    except ValueError:
        return source


def _section_record(section: Section, root: Path) -> dict:
    return {
        "skill": section.skill,
        "heading": section.heading,
        "body": section.body,
        "source": str(_relative_source(section.source, root)),
        "line": section.line,
    }


def analysis_record(analysis: Analysis, root: Path) -> dict:
    return {
        "skill_count": analysis.skill_count,
        "section_count": len(analysis.sections),
        "match_count": len(analysis.matches),
        "cluster_count": len(analysis.clusters),
        "matches": [
            {
                "score": match.score,
                "left": _section_record(match.left, root),
                "right": _section_record(match.right, root),
            }
            for match in analysis.matches
        ],
        "clusters": [
            {
                "mean_score": cluster.mean_score,
                "sections": [_section_record(section, root) for section in cluster.sections],
            }
            for cluster in analysis.clusters
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="skill-library root to scan recursively")
    parser.add_argument("--output", type=Path, required=True, help="Markdown report path")
    parser.add_argument("--json-output", type=Path, help="optional machine-readable result path")
    parser.add_argument("--min-chars", type=int, default=120)
    parser.add_argument("--neighbors", type=int, default=3)
    parser.add_argument("--min-score", type=float, default=0.78)
    parser.add_argument("--min-skills", type=int, default=3)
    parser.add_argument("--top-matches", type=int, default=50)
    args = parser.parse_args()

    from mcp_server.embedding import build_embedding

    root = args.root.resolve()
    analysis = analyze_corpus(
        root,
        build_embedding(),
        MatchConfig(
            min_chars=args.min_chars,
            neighbors=args.neighbors,
            min_score=args.min_score,
            min_skills=args.min_skills,
        ),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        render_markdown(analysis, root, top_matches=args.top_matches),
        encoding="utf-8",
    )
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(analysis_record(analysis, root), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(
        f"{analysis.skill_count} skills, {len(analysis.sections)} sections, "
        f"{len(analysis.matches)} matches, {len(analysis.clusters)} clusters"
    )


if __name__ == "__main__":
    main()
