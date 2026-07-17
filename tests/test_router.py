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


def _skill(name, description, **metadata):
    defaults = {
        "harnesses": ["claude", "codex"], "platforms": ["macos", "linux", "windows"],
        "scopes": ["global"], "path_patterns": [], "required_tools": [], "required_mcps": [],
        "trust": "unknown", "activation": "automatic", "priority": 50, "conflicts": [],
    }
    defaults.update(metadata)
    return Skill(name, description, f"{name} body", f"/{name}/SKILL.md", revision=f"rev-{name}",
                 root=f"/{name}", metadata=defaults)


def test_route_returns_one_body_and_bounded_bodyless_alternatives():
    router = Router([
        _skill("pdf", "Merge and edit PDF documents."),
        _skill("docs", "Write and edit text documents."),
        _skill("sheets", "Analyze spreadsheet data."),
    ])
    result = router.route("merge PDF files", "codex", "/tmp", min_score=0.0)
    assert result["match"] == "pdf"
    assert result["skill_body"] == "pdf body"
    assert result["skill_root"] == "/pdf"
    assert result["revision"] == "rev-pdf"
    assert len(result["alternatives"]) <= 2
    assert all("skill_body" not in item for item in result["alternatives"])


def test_route_returns_clean_no_match_below_threshold():
    result = Router([_skill("pdf", "Merge PDF documents.")]).route(
        "photosynthesis", "codex", "/tmp", min_score=0.99
    )
    assert result["match"] is None
    assert result["skill_body"] == "" and result["skill_root"] is None
    assert "threshold" in result["reason"]


def test_route_novel_flag_signals_weak_strong_escalation():
    router = Router([_skill("pdf", "Merge and edit PDF documents.")])
    assert router.route("merge PDF files", "codex", "/tmp", min_score=0.0)["novel"] is False
    # no match but still related -> compose/extend territory, weak model keeps serving
    related = router.route("split a PDF into chapters", "codex", "/tmp",
                           min_score=0.99, related_score=0.0)
    assert related["match"] is None and related["novel"] is False
    # nothing even related -> the harness should escalate to its strong model + create_skill
    novel = router.route("photosynthesis in plants", "codex", "/tmp",
                         min_score=0.99, related_score=0.98)
    assert novel["match"] is None and novel["novel"] is True
    # an empty/incompatible candidate set is also novel
    assert Router([]).route("anything", "codex", "/tmp")["novel"] is True


@pytest.mark.parametrize("skill_metadata,context", [
    ({"harnesses": ["claude"]}, {"harness": "codex"}),
    ({"platforms": ["linux"]}, {"platform": "macos"}),
    ({"activation": "manual"}, {}),
    ({"trust": "blocked"}, {}),
    ({"required_tools": ["browser"]}, {"available_tools": ["bash"]}),
    ({"required_mcps": ["github"]}, {"available_mcps": []}),
    ({"scopes": ["project"], "path_patterns": ["*/wanted/*"]}, {"cwd": "/tmp/other/project"}),
])
def test_route_filters_incompatible_skills_before_ranking(skill_metadata, context):
    router = Router([_skill("blocked", "Merge PDF documents.", **skill_metadata)])
    args = {"task": "merge PDF", "harness": "codex", "cwd": "/tmp/project", "platform": "macos",
            "min_score": 0.0, **context}
    assert router.route(**args)["match"] is None


def test_route_uses_harness_variant_body():
    skill = _skill("pdf", "Merge PDF documents.")
    skill = Skill(**{**skill.__dict__, "variants": {"codex": "codex-specific body"}})
    result = Router([skill]).route("merge PDF", "codex", "/tmp", min_score=0.0)
    assert result["skill_body"] == "codex-specific body"


def test_route_uses_priority_then_name_for_equal_scores():
    low = _skill("low", "Identical routing description.", priority=10)
    high = _skill("high", "Identical routing description.", priority=90)
    result = Router([low, high]).route("identical routing description", "codex", "/tmp", min_score=0.0)
    assert result["match"] == "high"


def test_project_path_scope_matches_files_below_cwd(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')")
    router = Router([_skill("python", "Debug Python code.", scopes=["project"],
                            path_patterns=["**/*.py"])])
    result = router.route("debug python", "codex", str(tmp_path), min_score=0.0)
    assert result["match"] == "python"


def test_conflicting_skills_do_not_both_appear_in_ranked_result():
    one = _skill("one", "Same routing text.", conflicts=["two"])
    two = _skill("two", "Same routing text.", priority=40)
    result = Router([one, two]).route("same routing text", "codex", "/tmp", min_score=0.0)
    ranked = [result["match"], *[item["name"] for item in result["alternatives"]]]
    assert not ({"one", "two"} <= set(ranked))
