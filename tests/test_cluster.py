"""Unit tests for skill clustering (embeddings mocked; the numpy maths is exercised for real)."""
import numpy as np
import pytest

from ingot.optimize import cluster as C


def _blobs(seed=0):
    """Three well-separated groups on the unit sphere — clustering that cannot recover these is
    not going to recover anything subtler."""
    rng = np.random.default_rng(seed)
    centres = np.array([[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]])
    pts = np.vstack([c + rng.normal(0, 0.05, (8, 3)) for c in centres])
    return C._normalise(pts)


def test_normalise_gives_unit_vectors():
    v = C._normalise(np.array([[3.0, 4.0], [0.0, 2.0]]))
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0)


def test_normalise_survives_a_zero_vector():
    """A zero row would divide by zero and poison every downstream distance with nan."""
    assert np.isfinite(C._normalise(np.zeros((1, 4)))).all()


def test_kmeans_recovers_separated_groups():
    labels, _ = C.kmeans(_blobs(), 3)
    groups = [set(np.where(labels == j)[0]) for j in range(3)]
    assert sorted(len(g) for g in groups) == [8, 8, 8]


def test_kmeans_is_deterministic_for_one_library():
    """The view polls. Buckets that reshuffle between identical runs are not something a reader
    can build a mental model of."""
    v = _blobs()
    assert np.array_equal(C.kmeans(v, 3)[0], C.kmeans(v, 3)[0])


def test_kmeans_never_returns_fewer_buckets_than_asked():
    """An emptied centre must be re-seeded. Left alone it collapses the run to k-1 and the caller
    silently gets a different k than the silhouette was computed for."""
    v = C._normalise(np.array([[1.0, 0.0], [1.0, 0.001], [1.0, 0.002], [1.0, 0.003]]))
    labels, centres = C.kmeans(v, 3)
    assert len(centres) == 3 and np.isfinite(centres).all()


def test_silhouette_prefers_the_true_group_count():
    v = _blobs()
    scores = {k: C.silhouette(v, C.kmeans(v, k)[0]) for k in (2, 3, 5)}
    assert scores[3] == max(scores.values())


def test_silhouette_is_undefined_for_one_bucket():
    assert C.silhouette(_blobs(), np.zeros(24, dtype=int)) == -1.0


def test_project_2d_returns_two_dimensions_and_is_centred():
    xy = C.project_2d(_blobs())
    assert xy.shape == (24, 2)
    assert np.allclose(xy.mean(0), 0, atol=1e-9)


def test_project_keeps_the_requested_number_of_components():
    assert C.project(_blobs(), 3).shape == (24, 3)


def test_reducing_before_clustering_beats_clustering_raw():
    """The reason CLUSTER_DIMS exists. In high dimensions over few points every pair sits at a
    similar distance and k-means has nothing to bite on — measured on the real 102-skill library,
    raw 1024d scored +0.05 against +0.28 over 10 components. Reproduced here with padded noise
    dimensions so the property is checked, not just remembered."""
    rng = np.random.default_rng(3)
    signal = _blobs(seed=2)
    noise = rng.normal(0, 0.6, (len(signal), 300))
    wide = C._normalise(np.hstack([signal, noise]))
    raw = max(C.silhouette(wide, C.kmeans(wide, k)[0]) for k in (3, 4, 5))
    reduced = C._normalise(C.project(wide, 10))
    cut = max(C.silhouette(reduced, C.kmeans(reduced, k)[0]) for k in (3, 4, 5))
    assert cut > raw


def test_top_terms_favours_what_is_distinctive_not_what_is_common():
    """Every skill says "use"; only one group says "invoice". Raw frequency returns the first."""
    texts = ["use invoice billing ledger", "use invoice payment ledger",
             "use pytest fixture mock", "use pytest assert mock"]
    assert "invoice" in C.top_terms(texts, [0, 1], k=3)
    assert "use" not in C.top_terms(texts, [0, 1], k=3)


def test_top_terms_drops_stopwords_and_short_tokens():
    texts = ["the and for a bb kubernetes", "the and for a bb postgres"]
    assert C.top_terms(texts, [0], k=5) == ["kubernetes"]


def test_label_for_names_a_bucket_from_its_terms():
    assert C.label_for(["aws", "lambda", "deploy", "iam"]) == "aws · lambda · deploy"
    assert C.label_for([]) == "unlabelled"


def test_skill_texts_lead_with_the_name_as_words():
    """Hyphenated names carry the topic in this library, and an embedder splits them better as
    words than as one token."""
    s = type("S", (), {"name": "aws-cdk", "description": "Infra as code."})()
    assert C.skill_texts([s]) == ["aws cdk. Infra as code."]


def test_build_refuses_a_library_too_small_to_cluster(monkeypatch):
    monkeypatch.setattr("ingot.mcp_server.registry.load_skills", lambda *a, **k: [])
    with pytest.raises(SystemExit, match="SKILL_ROUTER_PATHS"):
        C.build(log=lambda *a: None)


def test_build_shapes_the_payload_the_ui_reads(monkeypatch, tmp_path):
    names = ["billing-runbook", "invoice-audit", "pytest-fixtures", "mock-patterns",
             "k8s-deploy", "helm-charts", "prose-edit", "headline-cuts"]
    skills = [type("S", (), {"name": n, "description": n.replace("-", " "), "metadata": {}})()
              for n in names]
    monkeypatch.setattr("ingot.mcp_server.registry.load_skills", lambda *a, **k: skills)
    rng = np.random.default_rng(1)
    vecs = {n: rng.normal(size=6) + (i // 2) * 3 for i, n in enumerate(names)}
    monkeypatch.setattr("ingot.mcp_server.embedding.build_embedding",
                        lambda *a, **k: type("E", (), {"embed": lambda self, t: [
                            vecs[n] for n in names]})())
    monkeypatch.setattr(C, "K_MAX", 4)
    data = C.build(log=lambda *a: None)
    assert data["n_skills"] == 8 and data["clusters"]
    assert sum(c["size"] for c in data["clusters"]) == 8            # every skill lands somewhere
    assert data["clusters"] == sorted(data["clusters"], key=lambda c: -c["size"])
    member = data["clusters"][0]["members"][0]
    assert set(member) == {"name", "xy", "provenance"} and len(member["xy"]) == 2
    assert {m["name"] for c in data["clusters"] for m in c["members"]} == set(names)
