"""Optional ML prompt-injection guard — the vLLM Semantic Router jailbreak detector
(`llm-semantic-router/mmbert32k-jailbreak-detector-merged`, an mmBERT text classifier that runs on CPU).

This is an OPT-IN layer. It needs `transformers` + `torch` (heavy), which are deliberately NOT in the
base image so `docker compose up` stays light. Install `requirements-guard.txt` (or set
`SKILL_GUARD_MODEL`) to activate it; when the deps/model are unavailable it silently degrades to the
regex heuristic in `safety.py` — no crash. It complements, not replaces, that heuristic."""
import functools
import os

MODEL = os.environ.get("SKILL_GUARD_MODEL", "")  # empty = disabled by default (keeps base image light)
THRESHOLD = float(os.environ.get("SKILL_GUARD_THRESHOLD", "0.7"))  # semantic-router's default
# labels the classifier uses for "not an attack" — anything else above THRESHOLD is flagged
_BENIGN = {"benign", "safe", "clean", "negative", "normal", "label_0", "0"}


@functools.lru_cache(maxsize=1)
def _pipeline():
    if not MODEL:
        return None
    try:
        from transformers import pipeline
        return pipeline("text-classification", model=MODEL, device=-1, truncation=True)
    except Exception as e:  # transformers/torch not installed, or model download failed
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
