"""Group the library into category buckets from the embeddings the router already uses.

A merged library is a flat list of names. Provenance folders say where a skill came from, which is
a fact about its origin and not about what it does — "authored here" holds testing skills next to
finance skills. This clusters on meaning instead, so the console can show what the library is
actually about and where it is thin.

Not UMAP + HDBSCAN, which is what the same view uses over 100k prompts elsewhere — but the reduction
those bring is not optional. Clustering the raw 1024-d embeddings scored silhouette +0.05 and put
half the library in one bucket, because at that width every pair of a hundred points sits at
roughly the same distance. Over the top 10 principal components the same run scores +0.28. So:
cosine k-means on an SVD projection, numpy only, with the reduction doing the job UMAP does there.

Clustering is not cheap enough to run inside a request (it loads the embedding model), so this is a
command that writes runs/clusters.json and the UI serves the file.

    python -m ingot.optimize.cluster
"""
import json
import os
import re
from collections import Counter
from pathlib import Path

import numpy as np
from ingot import paths

CLUSTER_PATH = paths.runs() / "clusters.json"
# Enough buckets to separate concerns, few enough that each one still means something. Bounded
# because both ends degenerate: k=2 says nothing, k=n gives every skill its own bucket.
K_MIN, K_MAX = 4, 12
# Components to cluster over. Measured on the 102-skill library: raw 1024d scores silhouette
# +0.05, 10d +0.28, 20d +0.19, 40d +0.13. Keep enough signal to separate topics, few enough
# dimensions that distances still mean something.
CLUSTER_DIMS = 10
_WORD = re.compile(r"[a-z][a-z0-9+-]{2,}")
# Words that describe every skill in a library of instructions and so distinguish none of them.
_STOP = {
    "the", "and", "for", "when", "with", "this", "that", "you", "your", "use", "uses", "used",
    "using", "user", "from", "into", "not", "any", "are", "its", "has", "have", "was", "will",
    "can", "should", "must", "need", "needs", "want", "wants", "asks", "ask", "them", "they",
    "what", "why", "how", "who", "which", "than", "then", "there", "here", "over", "under",
    "before", "after", "each", "every", "some", "all", "one", "two", "new", "own", "out",
    "run", "runs", "get", "gets", "set", "sets", "make", "makes", "does", "done", "via",
    "skill", "skills", "task", "tasks", "work", "works", "write", "writes", "write-up",
    # Trigger-phrase vocabulary. A routing description is written to be matched ("Use whenever the
    # user asks…"), so this register appears in most of them and named a bucket "whenever · api".
    "whenever", "asking", "request", "requests", "mentions", "triggers", "trigger", "wants",
    "needed", "including", "instead", "rather", "across", "against", "within", "about",
}


def skill_texts(skills) -> list[str]:
    """What gets embedded. The name carries real signal in this library (`aws-cdk`,
    `writing-clearly`), so it leads, and the description is the routing trigger the router itself
    matches on — clustering the same text keeps the buckets consistent with routing behaviour."""
    return [f"{s.name.replace('-', ' ')}. {s.description}" for s in skills]


def _normalise(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.maximum(norms, 1e-12)


def kmeans(vectors: np.ndarray, k: int, seed: int = 0, iters: int = 60):
    """k-means++ init, Lloyd iterations, on unit vectors — so squared euclidean ranks the same as
    cosine. Seeded: the same library must produce the same buckets twice, or the view reshuffles
    under a poll and nobody can trust what they are looking at."""
    rng = np.random.default_rng(seed)
    n = len(vectors)
    centres = [vectors[rng.integers(n)]]
    for _ in range(k - 1):                      # k-means++: sample far from what is already chosen
        d2 = np.min(((vectors[:, None, :] - np.array(centres)[None]) ** 2).sum(-1), axis=1)
        total = d2.sum()
        centres.append(vectors[rng.choice(n, p=d2 / total) if total > 0 else rng.integers(n)])
    centres = np.array(centres)
    labels = np.zeros(n, dtype=int)
    for _ in range(iters):
        labels = np.argmin(((vectors[:, None, :] - centres[None]) ** 2).sum(-1), axis=1)
        moved = False
        for j in range(k):
            members = vectors[labels == j]
            if not len(members):                # an emptied centre is re-seeded, never left to
                members = vectors[rng.integers(n)][None]    # collapse the run to k-1 buckets
            centre = _normalise(members.mean(0, keepdims=True))[0]
            if not np.allclose(centre, centres[j]):
                centres[j], moved = centre, True
        if not moved:
            break
    return labels, centres


def silhouette(vectors: np.ndarray, labels: np.ndarray) -> float:
    """Mean silhouette, used only to pick k. A bucket count chosen by hand is a guess that ages
    badly as the library grows."""
    unique = np.unique(labels)
    if len(unique) < 2:
        return -1.0
    dist = np.sqrt(np.maximum(((vectors[:, None, :] - vectors[None]) ** 2).sum(-1), 0))
    scores = []
    for i in range(len(vectors)):
        same = labels == labels[i]
        same[i] = False
        if not same.any():
            continue                            # a singleton has no cohesion to measure
        a = dist[i][same].mean()
        b = min(dist[i][labels == other].mean() for other in unique if other != labels[i])
        scores.append((b - a) / max(a, b))
    return float(np.mean(scores)) if scores else -1.0


def project(vectors: np.ndarray, dims: int) -> np.ndarray:
    """Top `dims` principal components."""
    centred = vectors - vectors.mean(0, keepdims=True)
    _, _, vt = np.linalg.svd(centred, full_matrices=False)
    return centred @ vt[:dims].T


def project_2d(vectors: np.ndarray) -> np.ndarray:
    return project(vectors, 2)


def top_terms(texts: list[str], members: list[int], k: int = 6) -> list[str]:
    """Terms frequent inside the bucket and rare outside it. Raw frequency returns the words every
    skill uses, which names nothing."""
    inside, outside = Counter(), Counter()
    member_set = set(members)
    for i, text in enumerate(texts):
        words = {w for w in _WORD.findall(text.lower()) if w not in _STOP}
        (inside if i in member_set else outside).update(words)
    n_in, n_out = max(len(members), 1), max(len(texts) - len(members), 1)
    scored = {w: (c / n_in) - (outside[w] / n_out) for w, c in inside.items() if c > 1 or n_in < 3}
    return [w for w, _ in sorted(scored.items(), key=lambda kv: -kv[1])[:k]]


def label_for(terms: list[str]) -> str:
    return " · ".join(terms[:3]) if terms else "unlabelled"


def build(log=print) -> dict:
    from ingot.mcp_server.embedding import EMBED_MODEL, build_embedding
    from ingot.mcp_server.registry import load_skills

    skills = load_skills()
    if len(skills) < K_MIN:
        raise SystemExit(f"only {len(skills)} skills indexed; clustering needs at least {K_MIN}. "
                         f"Check SKILL_ROUTER_PATHS.")
    texts = skill_texts(skills)
    log(f"[cluster] embedding {len(skills)} skills with {EMBED_MODEL}…")
    vectors = _normalise(np.array([np.asarray(v, dtype=float)
                                   for v in build_embedding().embed(texts)]))

    # Cluster in the reduced space, not the raw one. At 1024 dimensions over a hundred points every
    # pair sits at a similar distance (measured on this library: cosine p25 0.42, median 0.51,
    # p75 0.60) and k-means has nothing to bite on — it scored silhouette +0.05 and put half the
    # library in one bucket. The same run over the top 10 components scores +0.28. This is what UMAP
    # is for in the version of this view that runs over 100k prompts; at this size the projection
    # the layout already needs is enough, taken a few more components deep.
    reduced = _normalise(project(vectors, min(CLUSTER_DIMS, len(skills) - 1)))

    upper = min(K_MAX, len(skills) // 2)
    best = None
    for k in range(K_MIN, max(K_MIN, upper) + 1):
        labels, centres = kmeans(reduced, k)
        score = silhouette(reduced, labels)
        log(f"[cluster] k={k:<3} silhouette {score:+.3f}")
        if best is None or score > best[0]:
            best = (score, k, labels, centres)
    score, k, labels, centres = best
    log(f"[cluster] chose k={k} (silhouette {score:+.3f})")

    xy = project_2d(vectors)
    clusters = []
    for j in range(k):
        members = [i for i in range(len(skills)) if labels[i] == j]
        if not members:
            continue
        terms = top_terms(texts, members)
        clusters.append({
            "id": j, "label": label_for(terms), "size": len(members), "top_terms": terms,
            "centroid_2d": [float(xy[members, 0].mean()), float(xy[members, 1].mean())],
            "members": sorted(({"name": skills[i].name,
                                "xy": [float(xy[i, 0]), float(xy[i, 1])],
                                "provenance": (skills[i].metadata or {}).get("provenance", "")}
                               for i in members), key=lambda m: m["name"]),
        })
    clusters.sort(key=lambda c: -c["size"])
    return {"version": 1, "n_skills": len(skills), "k": k, "silhouette": round(score, 4),
            "embedder": EMBED_MODEL, "clusterer": f"cosine k-means (k={k}, seeded)",
            "reducer": f"pca-{CLUSTER_DIMS}d cluster / pca-2d layout", "clusters": clusters}


def build_and_save(log=print) -> Path:
    data = build(log=log)
    CLUSTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLUSTER_PATH.write_text(json.dumps(data, indent=2))
    log(f"[cluster] {data['k']} buckets over {data['n_skills']} skills → {CLUSTER_PATH}")
    for c in data["clusters"]:
        log(f"  {c['size']:>3}  {c['label']}")
    return CLUSTER_PATH


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    build_and_save()
