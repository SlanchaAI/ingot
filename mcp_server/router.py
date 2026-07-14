"""Tier-1 embedding router: embed every skill's description once, then suggest the top-k skills for a
task by cosine similarity. CPU-only via fastembed (ONNX) — no GPU, so the demo is `docker compose up`.

Model is `EMBED_MODEL` (default bge-small — fast + tiny). Any fastembed model works, e.g.
`BAAI/bge-large-en-v1.5` (1024-dim, more accurate) at ~10x the per-query and index-build cost on CPU.
A stronger model like Qwen3-Embedding-0.6B belongs on the GPU/vLLM path, not this portable CPU demo."""
from __future__ import annotations
import os

import numpy as np
from fastembed import TextEmbedding

from .registry import Skill

_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")


class Router:
    def __init__(self, skills: list[Skill]):
        self.skills = skills
        self._embed = TextEmbedding(model_name=_MODEL)
        if not skills:  # empty library — don't normalize an empty matrix
            self._mat = np.zeros((0, 0), dtype=np.float32)
            return
        mat = np.array(list(self._embed.embed([s.description for s in skills])), dtype=np.float32)
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
        q = np.array(next(iter(self._embed.embed([task]))), dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-8)
        scores = self._mat @ q
        top = np.argsort(-scores)[:k]
        return [
            {"name": self.skills[i].name, "description": self.skills[i].description,
             "score": round(float(scores[i]), 3)}
            for i in top if scores[i] >= min_score
        ]
