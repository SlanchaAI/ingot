import pytest

from mcp_server.registry import load_skills, optimizable_components, skill_revision
from optimize import promote as P


def _library(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    skill = root / "pdf"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: pdf\ndescription: Merge PDFs.\n---\nold body\n")
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(root))
    monkeypatch.setattr(P, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(P, "REVISIONS_DIR", tmp_path / "revisions")
    return skill


def _pending(skill_dir, *, promotable=True):
    champion = optimizable_components(skill_dir)
    challenger = {**champion, "body": "new body"}
    current = load_skills(skill_dir.parent)[0]
    return {
        "skill": "pdf",
        "champion_components": champion,
        "challenger_components": challenger,
        "gate": {"promotable": promotable, "blocked": [] if promotable else ["regression"]},
        "evidence": {
            "champion": {"revision": current.revision},
            "challenger": {"revision": skill_revision(skill_dir, challenger)},
            "gate": {"promotable": promotable, "blocked": [] if promotable else ["regression"]},
        },
    }


def test_promote_refuses_blocked_evidence(tmp_path, monkeypatch):
    skill = _library(tmp_path, monkeypatch)
    P.save_pending("pdf", _pending(skill, promotable=False))
    with pytest.raises(ValueError, match="Behavioral CI gate blocked"):
        P.promote("pdf")
    assert "old body" in (skill / "SKILL.md").read_text()


def test_promote_refuses_stale_champion_revision(tmp_path, monkeypatch):
    skill = _library(tmp_path, monkeypatch)
    pending = _pending(skill)
    pending["evidence"]["champion"]["revision"] = "stale"
    P.save_pending("pdf", pending)
    with pytest.raises(ValueError, match="champion revision changed"):
        P.promote("pdf")


def test_promote_refuses_bundled_file_drift(tmp_path, monkeypatch):
    skill = _library(tmp_path, monkeypatch)
    (skill / "reference.md").write_text("version one")
    pending = _pending(skill)
    (skill / "reference.md").write_text("version two")
    P.save_pending("pdf", pending)
    with pytest.raises(ValueError, match="champion revision changed"):
        P.promote("pdf")


def test_promote_snapshots_previous_revision_and_swaps_challenger(tmp_path, monkeypatch):
    skill = _library(tmp_path, monkeypatch)
    pending = _pending(skill)
    old_revision = pending["evidence"]["champion"]["revision"]
    P.save_pending("pdf", pending)
    result = P.promote("pdf")
    assert "new body" in (skill / "SKILL.md").read_text()
    assert "old body" in (P.REVISIONS_DIR / "pdf" / old_revision / "SKILL.md").read_text()
    assert not P.pending_path("pdf").exists()
    assert old_revision in result
    assert load_skills(skill.parent)[0].revision == pending["evidence"]["challenger"]["revision"]


def test_failed_stage_write_leaves_live_skill_untouched(tmp_path, monkeypatch):
    skill = _library(tmp_path, monkeypatch)
    P.save_pending("pdf", _pending(skill))

    def fail_write(*args, **kwargs):
        raise RuntimeError("stage failed")

    monkeypatch.setattr(P, "write_components", fail_write)
    with pytest.raises(RuntimeError, match="stage failed"):
        P.promote("pdf")
    assert "old body" in (skill / "SKILL.md").read_text()


def test_write_components_rejects_symlink_escape(tmp_path):
    skill = tmp_path / "skill"
    outside = tmp_path / "outside"
    skill.mkdir(); outside.mkdir()
    (skill / "SKILL.md").write_text("---\nname: skill\ndescription: d\n---\nbody\n")
    (skill / "scripts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes skill root"):
        from mcp_server.registry import write_components
        write_components(skill, {"description": "d", "body": "b", "file:scripts/pwn.py": "bad"})
