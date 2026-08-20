"""Embedding backends for the router: local CPU ONNX or a remote GPU embedding server.

Default: the Qwen3-Embedding-0.6B q4 export, chosen on a 297-query drafted routing eval. It is an
asymmetric retrieval model: queries carry the official instruction prefix, documents (skill
descriptions) do not, and pooling is last-token.

The choice is a deliberate ranking-over-discrimination trade, measured on that eval:
  - RANKING (which skill is #1, the router's primary job): q4 serves the correct skill 80.1% vs
    bge-small's 72.7%, never dropping a correct match to novel (the extra matches land in the
    related/compose band, not lost), so no extra strong-model escalation.
  - DISCRIMINATION (route-vs-novel threshold sharpness): q4 is WORSE (best balanced-acc 0.782 vs
    bge 0.859); its compressed cosine range makes the direct/related/novel boundaries mushier.
Among Qwen quant levels, q4 (4-bit weight-only) both ranks best and is fastest (~15 ms/query on
CPU); q8 (dynamic activation quant) ranks no better than bge and separates worse (dominated).

Any fastembed model name (e.g. the previous default BAAI/bge-small-en-v1.5) still works as
`EMBED_MODEL`, embedded symmetrically exactly as before, but the routing thresholds are
calibrated per model (see docs/configuration.md), so override MIN_SCORE / RELATED_SCORE /
COLLISION_SCORE together with the model."""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np

EMBED_MODEL = os.environ.get("EMBED_MODEL", "onnx-community/Qwen3-Embedding-0.6B-ONNX")
EMBED_ONNX_FILE = os.environ.get("EMBED_ONNX_FILE", "onnx/model_q4.onnx")
EMBED_BACKEND = os.environ.get("EMBED_BACKEND", "local")
# The official Qwen3-Embedding retrieval instruction: applied to queries only, never documents.
QUERY_PREFIX = ("Instruct: Given a web search query, retrieve relevant passages that answer "
                "the query\nQuery: ")
_EOS = 151643  # <|endoftext|>, appended to every input per the official Qwen3-Embedding usage
_BATCH = 16


def is_qwen_onnx(model: str) -> bool:
    return "qwen3-embedding" in model.lower() and "onnx" in model.lower()


def _baked_qwen(model: str, onnx_file: str) -> bool:
    """Whether this exact Qwen export was cached while the container image was built."""
    return (model == os.environ.get("BAKED_EMBED_MODEL")
            and onnx_file == os.environ.get("BAKED_EMBED_ONNX_FILE"))


def _baked_fastembed(model: str) -> bool:
    """Whether this fastembed fallback was cached while the container image was built."""
    return model == os.environ.get("BAKED_FALLBACK_EMBED_MODEL")


class QwenOnnxEmbedding:
    """A Qwen3-Embedding ONNX export on ONNX Runtime CPU. Right padding with attention-mask-indexed
    last-token pooling (equivalent to the official left-padded pooling under causal attention: the
    last real token never attends to the pads after it), and an empty KV cache fed to the
    decoder-style export."""

    def __init__(self, model: str = EMBED_MODEL, onnx_file: str = EMBED_ONNX_FILE):
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer
        local_only = _baked_qwen(model, onnx_file)
        self._tokenizer = Tokenizer.from_file(
            hf_hub_download(model, "tokenizer.json", local_files_only=local_only))
        self._session = ort.InferenceSession(
            hf_hub_download(model, onnx_file, local_files_only=local_only),
                                             providers=["CPUExecutionProvider"])
        self._inputs = self._session.get_inputs()
        self._out = next(o.name for o in self._session.get_outputs() if "hidden" in o.name)

    def _feeds(self, ids_batch: list[list[int]]):
        width = max(len(ids) for ids in ids_batch)
        mask = np.array([[1] * len(ids) + [0] * (width - len(ids)) for ids in ids_batch],
                        dtype=np.int64)
        feeds = {"input_ids": np.array([ids + [0] * (width - len(ids)) for ids in ids_batch],
                                       dtype=np.int64)}
        for spec in self._inputs:
            if spec.name == "attention_mask":
                feeds[spec.name] = mask
            elif spec.name == "position_ids":
                feeds[spec.name] = np.tile(np.arange(width, dtype=np.int64), (len(ids_batch), 1))
            elif spec.name.startswith("past_key_values."):
                _, kv_heads, _, head_dim = spec.shape
                feeds[spec.name] = np.zeros((len(ids_batch), kv_heads, 0, head_dim),
                                            dtype=np.float32)
        return feeds, mask

    def _run(self, texts: list[str]) -> list[np.ndarray]:
        vectors: list[np.ndarray] = []
        for start in range(0, len(texts), _BATCH):
            encodings = self._tokenizer.encode_batch(texts[start:start + _BATCH])
            ids_batch = [(list(e.ids) + [_EOS] if not e.ids or e.ids[-1] != _EOS else list(e.ids))
                         for e in encodings]
            feeds, mask = self._feeds(ids_batch)
            hidden = self._session.run([self._out], feeds)[0].astype(np.float32)
            last = mask.sum(axis=1) - 1
            pooled = hidden[np.arange(len(ids_batch)), last]
            vectors.extend(pooled / (np.linalg.norm(pooled, axis=1, keepdims=True) + 1e-8))
        return vectors

    def embed(self, texts) -> list[np.ndarray]:
        """Document embeddings (skill descriptions): no prefix."""
        return self._run(list(texts))

    def embed_query(self, texts) -> list[np.ndarray]:
        """Query embeddings (user tasks): the retrieval instruction prefix."""
        return self._run([QUERY_PREFIX + text for text in texts])


class FastembedEmbedding:
    """fastembed passthrough: symmetric embedding with no query prefix, exactly the router's
    pre-Qwen behavior, so an EMBED_MODEL override to a fastembed model changes nothing else."""

    def __init__(self, model: str = EMBED_MODEL):
        from fastembed import TextEmbedding
        self._model = TextEmbedding(model_name=model, local_files_only=_baked_fastembed(model))

    def embed(self, texts):
        return self._model.embed(list(texts))

    embed_query = embed


class RemoteQwenEmbedding:
    """OpenAI-compatible Qwen embedding server.

    The server stays outside the MCP process so the CPU image remains small and portable. GPU
    selection is explicit: requesting this backend without a reachable server fails instead of
    silently changing model quality and invalidating calibrated routing thresholds.
    """

    def __init__(self, model: str, base_url: str, timeout: float = 60):
        self._model = model
        self._url = base_url.rstrip("/") + "/embeddings"
        self._timeout = timeout
        self.identity = f"remote:{model}@{base_url.rstrip('/')}"

    def _run(self, texts: list[str]) -> list[np.ndarray]:
        if not texts:
            return []
        body = json.dumps({
            "model": self._model,
            "input": texts,
            "encoding_format": "float",
        }).encode()
        request = Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer no-key"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"embedding server unavailable at {self._url}: {exc}") from exc
        try:
            ordered = sorted(payload["data"], key=lambda item: item["index"])
            vectors = [np.asarray(item["embedding"], dtype=np.float32) for item in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("embedding server returned an invalid response") from exc
        if len(vectors) != len(texts):
            raise RuntimeError("embedding server returned the wrong vector count")
        return vectors

    def embed(self, texts) -> list[np.ndarray]:
        return self._run(list(texts))

    def embed_query(self, texts) -> list[np.ndarray]:
        return self._run([QUERY_PREFIX + text for text in texts])


def build_embedding(model: str = EMBED_MODEL):
    backend = os.environ.get("EMBED_BACKEND", EMBED_BACKEND).lower()
    if backend == "remote":
        base_url = os.environ.get("EMBED_BASE_URL", "").strip()
        if not base_url:
            raise ValueError("EMBED_BASE_URL is required when EMBED_BACKEND=remote")
        remote_model = os.environ.get("EMBED_REMOTE_MODEL", "").strip()
        if not remote_model:
            raise ValueError("EMBED_REMOTE_MODEL is required when EMBED_BACKEND=remote")
        try:
            timeout = float(os.environ.get("EMBED_TIMEOUT_SECONDS", "60"))
        except ValueError as exc:
            raise ValueError("EMBED_TIMEOUT_SECONDS must be a number") from exc
        return RemoteQwenEmbedding(remote_model, base_url, timeout)
    if backend != "local":
        raise ValueError("EMBED_BACKEND must be local or remote")
    return QwenOnnxEmbedding(model) if is_qwen_onnx(model) else FastembedEmbedding(model)
