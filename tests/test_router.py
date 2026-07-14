"""Unit tests for the embedding router (real fastembed model, a handful of synthetic skills).
Validates the retrieval + threshold behavior that routing, compose-awareness, and the description
collision check all depend on."""
import pytest

from mcp_server.registry import Skill
from mcp_server.router import Router

SKILLS = [
    Skill("pdf", "Merge, split, and extract text from PDF files and documents.", "body", "p"),
    Skill("xlsx", "Analyze Excel spreadsheets, pivot tables, and tabular data.", "body", "x"),
    Skill("email", "Compose and send email messages and manage an inbox.", "body", "e"),
]


@pytest.fixture(scope="module")
def router():
    return Router(SKILLS)


def test_suggest_ranks_the_relevant_skill_first(router):
    top = router.suggest("combine several PDF documents into one", k=3, min_score=0.0)
    assert top[0]["name"] == "pdf" and top[0]["score"] >= top[-1]["score"]  # sorted, pdf on top


def test_suggest_min_score_filters_weak_matches(router):
    # an unrelated query shouldn't clear a high routing threshold for any skill
    assert router.suggest("photosynthesis in plants", k=3, min_score=0.9) == []


def test_suggest_respects_k(router):
    assert len(router.suggest("spreadsheet data", k=1, min_score=0.0)) == 1


def test_nearest_returns_argmax(router):
    name, score = router.nearest("send an email to the team")
    assert name == "email" and 0.0 <= score <= 1.0


def test_nearest_empty_router_is_safe():
    assert Router([]).nearest("anything") == ("", 0.0)
