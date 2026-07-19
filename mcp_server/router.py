"""Tier-1 embedding router: embed every skill's description once, then suggest the top-k skills for a
task by cosine similarity. CPU-only via fastembed (ONNX) — no GPU, so the demo is `docker compose up`.

Model is `EMBED_MODEL` (default bge-small — fast + tiny). Any fastembed model works, e.g.
`BAAI/bge-large-en-v1.5` (1024-dim, more accurate) at ~10x the per-query and index-build cost on CPU.
A stronger model like Qwen3-Embedding-0.6B belongs on the GPU/vLLM path, not this portable CPU demo."""
from __future__ import annotations
import os
import sys
import threading
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding

from .registry import Skill

_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")


class Router:
    _vector_cache: dict[tuple[str, str], np.ndarray] = {}
    _cache_lock = threading.Lock()

    def __init__(self, skills: list[Skill]):
        self.skills = skills
        self._embed = TextEmbedding(model_name=_MODEL)
        if not skills:  # empty library — don't normalize an empty matrix
            self._mat = np.zeros((0, 0), dtype=np.float32)
            return
        keys = [(_MODEL, skill.description) for skill in skills]
        with self._cache_lock:
            missing = list(dict.fromkeys(key for key in keys if key not in self._vector_cache))
        if missing:
            vectors = self._embed.embed([description for _, description in missing])
            with self._cache_lock:
                for key, vector in zip(missing, vectors):
                    self._vector_cache[key] = np.asarray(vector, dtype=np.float32)
        with self._cache_lock:
            mat = np.array([self._vector_cache[key] for key in keys], dtype=np.float32)
        self._mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-8)

    def nearest(self, text: str) -> tuple[str, float]:
        """The most similar existing skill to `text` and its cosine score — used to reject a new
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
        q = np.array(next(iter(self._embed.embed([task]))), dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-8)
        scores = self._mat @ q
        top = np.argsort(-scores)[:k]
        return [
            {"name": self.skills[i].name, "description": self.skills[i].description,
             "score": round(float(scores[i]), 3)}
            for i in top if scores[i] >= min_score
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

    def route(self, task: str, harness: str, cwd: str, available_tools=(), available_mcps=(),
              platform: str | None = None, min_score: float = 0.65,
              related_score: float = 0.45) -> dict:
        """Filter compatible skills, rank them locally, and return at most one instruction body.
        `novel` is the escalation signal for the calling harness: True when nothing compatible is
        even related (best score below `related_score`) — the case where a weak/strong setup should
        serve with the strong model, then queue a candidate for human review."""
        eligible = [s for s in self.skills if self._compatible(
            s, harness.lower(), cwd, set(available_tools), set(available_mcps), self._platform(platform)
        )]
        empty = {
            "match": None, "related_match": None, "score": 0.0,
            "reason": "no compatible skill candidates",
            "skill_body": "", "skill_root": None, "revision": None, "alternatives": [],
            "novel": True,
        }
        if not eligible:
            return empty

        q = np.array(next(iter(self._embed.embed([task]))), dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-8)
        index_by_name = {skill.name: i for i, skill in enumerate(self.skills)}
        ranked = sorted(
            ((skill, float(self._mat[index_by_name[skill.name]] @ q)) for skill in eligible),
            key=lambda pair: (-pair[1], -int(pair[0].metadata.get("priority", 50)), pair[0].name),
        )
        conflict_free = []
        for candidate in ranked:
            skill = candidate[0]
            if any(skill.name in set(selected.metadata.get("conflicts", [])) or
                   selected.name in set(skill.metadata.get("conflicts", []))
                   for selected, _ in conflict_free):
                continue
            conflict_free.append(candidate)
        top, score = conflict_free[0]
        alternatives = [
            {"name": skill.name, "score": round(candidate_score, 3),
             "reason": f"compatible alternative; cosine {candidate_score:.3f}"}
            for skill, candidate_score in conflict_free[1:3]
        ]
        if score < min_score:
            if score < related_score:
                related = [{"name": top.name, "score": round(score, 3),
                            "reason": f"best compatible candidate; cosine {score:.3f}"},
                           *alternatives]
                return {**empty, "score": round(score, 3),
                        "reason": (f"best compatible score {score:.3f} below related threshold "
                                   f"{related_score:.3f}"),
                        "alternatives": related[:3]}
            return {
                "match": None,
                "related_match": top.name,
                "score": round(score, 3),
                "reason": (f"best compatible score {score:.3f} below direct threshold "
                           f"{min_score:.3f}; loaded for compose or extend"),
                "skill_body": top.body_for(harness.lower()),
                "skill_root": top.root or str(os.path.dirname(top.path)),
                "revision": top.revision or None,
                "alternatives": alternatives,
                "novel": False,
            }
        return {
            "match": top.name,
            "related_match": None,
            "score": round(score, 3),
            "reason": f"compatible {harness.lower()} skill; cosine {score:.3f}",
            "skill_body": top.body_for(harness.lower()),
            "skill_root": top.root or str(os.path.dirname(top.path)),
            "revision": top.revision or None,
            "alternatives": alternatives,
            "novel": False,
        }
