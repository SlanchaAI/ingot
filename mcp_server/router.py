"""Embedding router: cache description and bounded approved-content vectors, then suggest the
top-k skills for a task by cosine similarity. CPU-only ONNX, no GPU, so the demo is
`docker compose up`.

Model is `EMBED_MODEL` (default Qwen3-Embedding-0.6B q4, ~15 ms/query on CPU; queries get the
retrieval instruction prefix, descriptions don't). Any fastembed model name also works (e.g. the
previous default `BAAI/bge-small-en-v1.5`, ~4 ms/query), but recalibrate MIN_SCORE /
RELATED_SCORE / COLLISION_SCORE with the model (mcp_server/embedding.py)."""
from __future__ import annotations
import os
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .embedding import EMBED_MODEL as _MODEL, build_embedding
from .registry import Skill


@dataclass(frozen=True)
class _RankedSkill:
    skill: Skill
    score: float
    description_score: float
    content_score: float

    @property
    def matched_on(self) -> str:
        return "description" if self.description_score >= self.content_score else "content"

    def explanation(self) -> dict:
        return {
            "score_components": {
                "description": round(self.description_score, 3),
                "content": round(self.content_score, 3),
            },
            "matched_on": self.matched_on,
        }


class Router:
    _vector_cache: OrderedDict[tuple[str, str, str, str], np.ndarray] = OrderedDict()
    _vector_cache_limit = 4096
    _cache_lock = threading.Lock()

    def __init__(self, skills: list[Skill]):
        self.skills = skills
        try:
            self._body_chars = int(os.environ.get("ROUTER_BODY_CHARS", "1000"))
        except ValueError as exc:
            raise ValueError("ROUTER_BODY_CHARS must be an integer from 1 to 4000") from exc
        if not 1 <= self._body_chars <= 4000:
            raise ValueError("ROUTER_BODY_CHARS must be an integer from 1 to 4000")
        if not skills:  # empty library, don't normalize an empty matrix
            self._embed = None
            self._mat = np.zeros((0, 0), dtype=np.float32)
            return
        self._embed = build_embedding()
        backend = type(self._embed)
        self._embedding_identity = f"{backend.__module__}.{backend.__qualname__}"
        self._mat = self._matrix("description", [skill.description for skill in skills])

    def _matrix(self, representation: str, texts: list[str]) -> np.ndarray:
        keys = [(_MODEL, self._embedding_identity, representation, text) for text in texts]
        resolved = {}
        with self._cache_lock:
            missing = []
            for key in dict.fromkeys(keys):
                vector = self._vector_cache.get(key)
                if vector is None:
                    missing.append(key)
                    continue
                self._vector_cache.move_to_end(key)
                resolved[key] = vector
        if missing:
            vectors = list(self._embed.embed([text for _, _, _, text in missing]))
            if len(vectors) != len(missing):
                raise RuntimeError("embedding backend returned the wrong vector count")
            generated = {
                key: np.asarray(vector, dtype=np.float32)
                for key, vector in zip(missing, vectors)
            }
            resolved.update(generated)
            with self._cache_lock:
                for key, vector in generated.items():
                    self._vector_cache[key] = vector
                    self._vector_cache.move_to_end(key)
                while len(self._vector_cache) > self._vector_cache_limit:
                    self._vector_cache.popitem(last=False)
        mat = np.array([resolved[key] for key in keys], dtype=np.float32)
        return mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-8)

    def _content_text(self, skill: Skill, harness: str) -> str:
        return (
            f"Skill: {skill.name}\n"
            f"Description: {skill.description}\n"
            f"Instructions:\n{skill.body_for(harness)[:self._body_chars]}"
        )

    def _ranked(self, task: str, harness: str, skills: list[Skill]) -> list[_RankedSkill]:
        if not skills:
            return []
        query = np.array(next(iter(self._embed.embed_query([task]))), dtype=np.float32)
        query = query / (np.linalg.norm(query) + 1e-8)
        index_by_name = {skill.name: index for index, skill in enumerate(self.skills)}
        content = self._matrix(
            f"content:{harness or 'default'}",
            [self._content_text(skill, harness) for skill in skills],
        )
        ranked = []
        for content_index, skill in enumerate(skills):
            description_score = float(self._mat[index_by_name[skill.name]] @ query)
            content_score = float(content[content_index] @ query)
            ranked.append(_RankedSkill(
                skill=skill,
                score=max(description_score, content_score),
                description_score=description_score,
                content_score=content_score,
            ))
        return sorted(
            ranked,
            key=lambda item: (
                -item.score, -int(item.skill.metadata.get("priority", 50)), item.skill.name
            ),
        )

    def nearest(self, text: str) -> tuple[str, float]:
        """The most similar existing skill to `text` and its cosine score, used to reject a new
        skill whose description near-duplicates (shadows) an existing one's routing."""
        if not self.skills:
            return "", 0.0
        q = np.array(next(iter(self._embed.embed([text]))), dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-8)
        scores = self._mat @ q
        i = int(np.argmax(scores))
        return self.skills[i].name, float(scores[i])

    def suggest(self, task: str, k: int = 5, min_score: float = 0.0) -> list[dict]:
        if not self.skills:
            return []
        return [
            {
                "name": item.skill.name,
                "description": item.skill.description,
                "score": round(item.score, 3),
                **item.explanation(),
            }
            for item in self._ranked(task, "", self.skills)[:k] if item.score >= min_score
        ]

    @staticmethod
    def _platform(value: str | None) -> str:
        value = (value or sys.platform).lower()
        if value.startswith("darwin") or value == "macos":
            return "macos"
        if value.startswith("win"):
            return "windows"
        return "linux" if value.startswith("linux") else value

    @staticmethod
    def _compatible(skill: Skill, harness: str, cwd: str, available_tools: set[str],
                    available_mcps: set[str], platform: str) -> bool:
        meta = skill.metadata or {}
        if harness not in meta.get("harnesses", ["claude", "codex"]):
            return False
        if platform not in meta.get("platforms", ["macos", "linux", "windows"]):
            return False
        if meta.get("activation", "automatic") != "automatic" or meta.get("trust") == "blocked":
            return False
        if not set(meta.get("required_tools", [])).issubset(available_tools):
            return False
        if not set(meta.get("required_mcps", [])).issubset(available_mcps):
            return False
        scopes = meta.get("scopes", ["global"])
        if "global" not in scopes:
            patterns = meta.get("path_patterns", [])
            if "project" not in scopes or not patterns:
                return False
            project = Path(cwd).expanduser().resolve()
            if not project.is_dir() or not any(any(project.glob(pattern)) for pattern in patterns):
                return False
        return True

    def _eligible_ranking(self, task: str, harness: str, cwd: str,
                          available_tools: set[str], available_mcps: set[str],
                          platform: str) -> list[_RankedSkill]:
        eligible = [skill for skill in self.skills if self._compatible(
            skill, harness, cwd, available_tools, available_mcps, platform
        )]
        return self._ranked(task, harness, eligible)

    @staticmethod
    def _without_conflicts(ranked: list[_RankedSkill]) -> list[_RankedSkill]:
        selected = []
        for candidate in ranked:
            skill = candidate.skill
            if any(skill.name in set(existing.skill.metadata.get("conflicts", [])) or
                   existing.skill.name in set(skill.metadata.get("conflicts", []))
                   for existing in selected):
                continue
            selected.append(candidate)
        return selected

    @staticmethod
    def _alternatives(ranked: list[_RankedSkill]) -> list[dict]:
        return [
            {
                "name": item.skill.name,
                "score": round(item.score, 3),
                "reason": f"compatible alternative; {item.matched_on} cosine {item.score:.3f}",
                **item.explanation(),
            }
            for item in ranked[1:3]
        ]

    @staticmethod
    def _novel_response(score: float = 0.0, reason: str = "no compatible skill candidates",
                        alternatives: list[dict] | None = None,
                        candidate: _RankedSkill | None = None) -> dict:
        explanation = (
            candidate.explanation()
            if candidate is not None
            else {
                "matched_on": None,
                "score_components": {"description": 0.0, "content": 0.0},
            }
        )
        return {
            "match": None, "related_match": None, "score": round(score, 3),
            "reason": reason, "skill_body": "", "skill_root": None, "revision": None,
            "alternatives": alternatives or [], "novel": True, **explanation,
        }

    @staticmethod
    def _related_response(item: _RankedSkill, harness: str, min_score: float,
                          alternatives: list[dict]) -> dict:
        skill, score = item.skill, item.score
        return {
            "match": None, "related_match": skill.name, "score": round(score, 3),
            "reason": (f"best compatible score {score:.3f} below direct threshold "
                       f"{min_score:.3f}; matched on {item.matched_on}; "
                       "loaded for compose or extend"),
            "skill_body": skill.body_for(harness),
            "skill_root": skill.root or str(os.path.dirname(skill.path)),
            "revision": skill.revision or None, "alternatives": alternatives, "novel": False,
            **item.explanation(),
        }

    @staticmethod
    def _direct_response(item: _RankedSkill, harness: str,
                         alternatives: list[dict]) -> dict:
        skill, score = item.skill, item.score
        return {
            "match": skill.name, "related_match": None, "score": round(score, 3),
            "reason": f"compatible {harness} skill; {item.matched_on} cosine {score:.3f}",
            "skill_body": skill.body_for(harness),
            "skill_root": skill.root or str(os.path.dirname(skill.path)),
            "revision": skill.revision or None, "alternatives": alternatives, "novel": False,
            **item.explanation(),
        }

    def route(self, task: str, harness: str, cwd: str, available_tools=(), available_mcps=(),
              platform: str | None = None, min_score: float = 0.53,
              related_score: float = 0.37) -> dict:
        """Filter compatible skills, rank them locally, and return at most one instruction body.
        `novel` is the escalation signal for the calling harness: True when nothing compatible is
        even related (best score below `related_score`), the case where a weak/strong setup should
        serve with the strong model, then queue a candidate for human review."""
        harness = harness.lower()
        ranked = self._eligible_ranking(
            task, harness, cwd, set(available_tools), set(available_mcps), self._platform(platform)
        )
        if not ranked:
            return self._novel_response()
        ranked = self._without_conflicts(ranked)
        top = ranked[0]
        score = top.score
        alternatives = self._alternatives(ranked)
        if score < min_score:
            if score < related_score:
                related = [{"name": top.skill.name, "score": round(score, 3),
                            "reason": (f"best compatible candidate; {top.matched_on} "
                                       f"cosine {score:.3f}"),
                            **top.explanation()},
                           *alternatives]
                reason = (f"best compatible score {score:.3f} below related threshold "
                          f"{related_score:.3f}")
                return self._novel_response(score, reason, related[:3], top)
            return self._related_response(top, harness, min_score, alternatives)
        return self._direct_response(top, harness, alternatives)
