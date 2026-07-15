"""Optional ML prompt-injection guard — the vLLM Semantic Router jailbreak detector
(`llm-semantic-router/mmbert32k-jailbreak-detector-merged`, an mmBERT text classifier that runs on CPU).

Inference runs on **ONNX Runtime** via the model repo's bundled `onnx/model.onnx` export — no
`torch`/`transformers` needed. The deps (`onnxruntime`, `tokenizers`, `huggingface_hub`, `numpy`)
already ship with the base image via fastembed, so the guard is a pure config switch: set
`SKILL_GUARD_MODEL` to activate it (the ~1.2GB model downloads to the HF cache on first use).
When the model is unavailable it silently degrades to the regex heuristic in `safety.py` — no
crash. It complements, not replaces, that heuristic."""
import functools
import os

MODEL = os.environ.get("SKILL_GUARD_MODEL", "")  # empty = disabled by default (no surprise 1.2GB download)
THRESHOLD = float(os.environ.get("SKILL_GUARD_THRESHOLD", "0.7"))  # semantic-router's default
ONNX_FILE = os.environ.get("SKILL_GUARD_ONNX_FILE", "onnx/model.onnx")  # fp32 CPU export in the repo
# labels the classifier uses for "not an attack" — anything else above THRESHOLD is flagged
_BENIGN = {"benign", "safe", "clean", "negative", "normal", "label_0", "0"}


@functools.lru_cache(maxsize=1)
def _pipeline():
    """Build the classifier callable: text -> [{"label": ..., "score": ...}] (top-1, softmax prob).
    Same result shape as a transformers text-classification pipeline, so `check()` is agnostic."""
    if not MODEL:
        return None
    try:
        import json

        import numpy as np
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        onnx_dir = ONNX_FILE.rsplit("/", 1)[0] + "/" if "/" in ONNX_FILE else ""
        session = ort.InferenceSession(hf_hub_download(MODEL, ONNX_FILE),
                                       providers=["CPUExecutionProvider"])
        tokenizer = Tokenizer.from_file(hf_hub_download(MODEL, f"{onnx_dir}tokenizer.json"))
        with open(hf_hub_download(MODEL, f"{onnx_dir}config.json")) as f:
            config = json.load(f)
        id2label = {int(k): str(v) for k, v in (config.get("id2label") or {}).items()}
        tokenizer.enable_truncation(max_length=min(int(config.get("max_position_embeddings", 8192)), 8192))
        input_names = {i.name for i in session.get_inputs()}

        def classify(text: str) -> list[dict]:
            enc = tokenizer.encode(text)
            feeds = {"input_ids": np.array([enc.ids], dtype=np.int64)}
            if "attention_mask" in input_names:
                feeds["attention_mask"] = np.array([enc.attention_mask], dtype=np.int64)
            logits = session.run(None, feeds)[0][0]
            probs = np.exp(logits - logits.max())
            probs /= probs.sum()
            top = int(probs.argmax())
            return [{"label": id2label.get(top, str(top)), "score": float(probs[top])}]

        return classify
    except Exception as e:  # model download failed, or the repo has no ONNX export
        print(f"[guard] prompt-injection classifier unavailable ({e.__class__.__name__}) — "
              f"using regex heuristic only", flush=True)
        return None


def available() -> bool:
    return _pipeline() is not None


def check(text: str) -> str | None:
    """Return a rejection reason if the classifier flags the text as an injection/jailbreak above
    THRESHOLD, else None (also None when the model isn't enabled/installed)."""
    clf = _pipeline()
    if clf is None:
        return None
    try:
        result = clf(text[:4000])[0]  # cap to the model's context
    except Exception:
        return None
    label = str(result.get("label", "")).lower()
    score = float(result.get("score", 0.0))
    if score >= THRESHOLD and label not in _BENIGN:
        return f"flagged by the prompt-injection classifier ({label}, {score:.2f})"
    return None
