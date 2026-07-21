"""Embedding backends: model selection, and the Qwen ONNX pooling/prefix contract with a stubbed
session (no model download, no onnxruntime inference)."""
from types import SimpleNamespace

import numpy as np

from mcp_server import embedding as E


def test_backend_selection_by_model_name():
    assert E.is_qwen_onnx("onnx-community/Qwen3-Embedding-0.6B-ONNX")
    assert E.is_qwen_onnx("someone/Qwen3-Embedding-4B-ONNX")
    assert not E.is_qwen_onnx("BAAI/bge-small-en-v1.5")
    assert not E.is_qwen_onnx("Qwen/Qwen3-Embedding-0.6B")  # torch checkpoint, not the ONNX export


class _Enc(SimpleNamespace):
    pass


class _StubTokenizer:
    def encode_batch(self, texts):
        # token count = word count; ids are the word lengths (arbitrary but deterministic)
        return [_Enc(ids=[len(w) for w in t.split()]) for t in texts]


class _StubSession:
    """Records feeds; returns a hidden-state tensor where hidden[b, t] = [b*100 + t] so the test
    can verify exactly which token position was pooled."""

    def __init__(self):
        self.feeds = None

    def run(self, outputs, feeds):
        self.feeds = feeds
        batch, width = feeds["input_ids"].shape
        # second dim is a constant 1 so a wrongly-pooled position produces a distinct normalized
        # vector (a 1-dim positive value would normalize to [1.0] from any position and hide it)
        hidden = np.zeros((batch, width, 2), dtype=np.float32)
        for b in range(batch):
            for t in range(width):
                hidden[b, t] = [b * 100 + t, 1]
        return [hidden]


def _stubbed():
    backend = E.QwenOnnxEmbedding.__new__(E.QwenOnnxEmbedding)
    backend._tokenizer = _StubTokenizer()
    backend._session = _StubSession()
    backend._inputs = [
        SimpleNamespace(name="input_ids", shape=["batch", "seq"], type="tensor(int64)"),
        SimpleNamespace(name="attention_mask", shape=["batch", "seq"], type="tensor(int64)"),
        SimpleNamespace(name="past_key_values.0.key",
                        shape=["batch", 8, "past", 128], type="tensor(float)"),
    ]
    backend._out = "last_hidden_state"
    return backend


def test_qwen_pools_last_real_token_and_appends_eos():
    backend = _stubbed()
    vectors = backend.embed(["one two three", "one"])  # 3+eos=4 tokens vs 1+eos=2 tokens
    ids = backend._session.feeds["input_ids"]
    assert ids.shape == (2, 4)
    assert ids[0][-1] == E._EOS and ids[1][1] == E._EOS      # eos appended before padding
    assert list(backend._session.feeds["attention_mask"][1]) == [1, 1, 0, 0]
    # exact normalized vectors prove the pooled position: row 0's last real token is t=3 (value
    # [3, 1]), row 1's is t=1 (value [101, 1]) -- a pad position would give a different vector
    np.testing.assert_allclose(vectors[0], [3 / np.sqrt(10), 1 / np.sqrt(10)], rtol=1e-5)
    np.testing.assert_allclose(vectors[1], [101 / np.sqrt(10202), 1 / np.sqrt(10202)], rtol=1e-5)
    kv = backend._session.feeds["past_key_values.0.key"]
    assert kv.shape == (2, 8, 0, 128) and kv.dtype == np.float32


def test_qwen_prefixes_queries_but_not_documents():
    backend = _stubbed()
    backend.embed(["merge pdfs"])
    doc_width = backend._session.feeds["input_ids"].shape[1]
    backend.embed_query(["merge pdfs"])
    query_width = backend._session.feeds["input_ids"].shape[1]
    assert query_width > doc_width  # the instruction prefix adds tokens to queries only


def test_qwen_tolerates_empty_text():
    backend = _stubbed()
    (vector,) = backend.embed([""])
    assert backend._session.feeds["input_ids"].shape == (1, 1)  # bare EOS, no crash
    assert np.isfinite(vector).all()


def test_fastembed_backend_has_no_query_prefix():
    # symmetric embedding is the pre-Qwen contract fastembed overrides rely on
    assert E.FastembedEmbedding.embed_query is E.FastembedEmbedding.embed
