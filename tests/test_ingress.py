import json

import pytest

from ingot.mcp_server import registry
from ingot.optimize import promote as P


def _store(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv("INGOT_LIBRARY", str(root))
    monkeypatch.setenv("INGOT_RUNS", str(tmp_path))
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(root))
    return root


def _proposal(**overrides):
    data = {
        "skill": "copywriting",
        "description": "Write clear conversion copy.",
        "body": "# Copywriting\n\nUse evidence and preserve supplied facts.",
        "files": {"references/frameworks.md": "# Frameworks\n\nPAS"},
        "frontmatter": {},
        "summary": "Add the vetted copywriting skill.",
        "source": "dotfiles-claude@c658aee",
        "producer": "skill-retrospective",
        "caller": "improve existing intake",
        "evidence": ["Package is hash-pinned.", "Catalog discovery passed."],
        "pressure_scenario": "A rushed draft must preserve locked facts.",
        "risk": "New instructions may route too broadly.",
        "verification_status": "passed",
        "verification_command": "python3 scripts/codex-skill-catalog.py list --repo .",
        "verification_result": "copywriting discovered",
    }
    data.update(overrides)
    return data


def test_new_skill_submission_is_visible_but_inert(tmp_path, monkeypatch):
    from ingot.optimize import ingress

    root = _store(tmp_path, monkeypatch)

    result = ingress.submit_skill_create(**_proposal())

    pending = P.load_pending("copywriting")
    assert result == {"status": "quarantined", "skill": "copywriting",
                      "proposal_id": pending["creation"]["proposal_id"],
                      "promotable": True}
    assert pending["kind"] == "creation"
    assert pending["champion_components"] == {}
    assert pending["challenger_components"] == {
        "description": "Write clear conversion copy.",
        "body": "# Copywriting\n\nUse evidence and preserve supplied facts.",
        "frontmatter": '{"description":"Write clear conversion copy.","name":"copywriting"}',
        "file:references/frameworks.md": "# Frameworks\n\nPAS",
    }
    assert pending["gate"]["kind"] == "new_skill_admission"
    assert not (root / "copywriting").exists()
    assert json.loads((tmp_path / "ingress-audit.jsonl").read_text())["action"] == "quarantine"


def test_creation_refuses_existing_skill_unsafe_files_and_unverified_input(tmp_path, monkeypatch):
    from ingot.optimize import ingress

    root = _store(tmp_path, monkeypatch)
    existing = root / "copywriting"
    existing.mkdir()
    registry.write_skill_md(existing / "SKILL.md",
                            {"name": "copywriting", "description": "Existing."}, "Body")

    with pytest.raises(ValueError, match="already exists"):
        ingress.submit_skill_create(**_proposal())
    (existing / "SKILL.md").unlink()
    existing.rmdir()
    with pytest.raises(ValueError, match="escapes skill root"):
        ingress.submit_skill_create(**_proposal(files={"../outside.md": "no"}))
    with pytest.raises(ValueError, match="verification_status"):
        ingress.submit_skill_create(**_proposal(verification_status="unavailable"))


def test_creation_promotion_is_reversible_to_absence(tmp_path, monkeypatch):
    from ingot.optimize import ingress

    root = _store(tmp_path, monkeypatch)
    ingress.submit_skill_create(**_proposal())

    promoted = P._activate_approved("copywriting", P.load_pending("copywriting"),
                                    actor="reviewer")
    created = root / "copywriting"
    assert "Added 'copywriting'" in promoted
    assert created.is_dir()
    assert "Use evidence" in (created / "SKILL.md").read_text()
    assert (created / "references/frameworks.md").read_text().endswith("PAS")
    assert not P.pending_path("copywriting").exists()

    removed = P._activate_rollback("copywriting", P.ABSENT_REVISION, actor="reviewer")
    assert "to absence" in removed
    assert not created.exists()

    created_revision = next(item["revision"] for item in P.list_revisions("copywriting")
                            if item["revision"] != P.ABSENT_REVISION)
    restored = P._activate_rollback("copywriting", created_revision, actor="reviewer")
    assert "Restored absent skill" in restored
    assert created.is_dir()
    assert "Use evidence" in (created / "SKILL.md").read_text()


def test_duplicate_creation_is_idempotent(tmp_path, monkeypatch):
    from ingot.optimize import ingress

    _store(tmp_path, monkeypatch)

    first = ingress.submit_skill_create(**_proposal())
    second = ingress.submit_skill_create(**_proposal())

    assert first["status"] == "quarantined"
    assert second == {**first, "status": "duplicate"}
    evidence = tmp_path / "evidence" / "copywriting" / f"creation-{first['proposal_id']}"
    assert (evidence / "evidence.json").is_file()
    assert len((tmp_path / "ingress-audit.jsonl").read_text().splitlines()) == 1


def test_creation_binds_normalized_full_frontmatter_and_staged_bytes(tmp_path, monkeypatch):
    from ingot.optimize import ingress

    root = _store(tmp_path, monkeypatch)
    router = {"harnesses": ["codex"], "platforms": ["macos"],
              "required_tools": ["rg"], "activation": "explicit"}
    ingress.submit_skill_create(**_proposal(
        description="Write  clear\nconversion copy.",
        frontmatter={"license": "MIT", "metadata": {"skill-router": router}},
    ))
    pending = P.load_pending("copywriting")
    expected = pending["evidence"]["challenger"]["revision"]

    P._activate_approved("copywriting", P.load_pending("copywriting"), actor="reviewer")

    active = registry.load_skills(root)[0]
    meta, _ = registry.parse_skill((root / "copywriting" / "SKILL.md").read_text(), "copywriting")
    assert active.revision == expected
    assert active.description == "Write clear conversion copy."
    assert active.metadata["harnesses"] == ["codex"]
    assert active.metadata["platforms"] == ["macos"]
    assert meta["license"] == "MIT"


def test_evidence_changes_are_not_aliased_as_duplicates(tmp_path, monkeypatch):
    from ingot.optimize import ingress

    _store(tmp_path, monkeypatch)
    first = ingress.submit_skill_create(**_proposal())

    with pytest.raises(ValueError, match="review slot is occupied"):
        ingress.submit_skill_create(**_proposal(risk="Different material risk."))
    assert P.load_pending("copywriting")["creation"]["proposal_id"] == first["proposal_id"]


def test_audit_failure_does_not_turn_a_queued_skill_into_a_false_refusal(tmp_path, monkeypatch,
                                                                        caplog):
    from ingot.optimize import ingress

    _store(tmp_path, monkeypatch)
    monkeypatch.setattr(ingress, "audit_file", lambda: tmp_path / "missing" / "audit.jsonl")
    monkeypatch.setattr(ingress, "_audit", lambda record: (_ for _ in ()).throw(OSError("full")))

    result = ingress.submit_skill_create(**_proposal())

    assert result["status"] == "quarantined"
    assert P.load_pending("copywriting") is not None
    assert "audit write failed" in caplog.text


@pytest.mark.parametrize("path", ["C:/x.md", "CON.md", "AUX/file.md", "bad\\name.md"])
def test_creation_rejects_nonportable_component_paths(tmp_path, monkeypatch, path):
    from ingot.optimize import ingress

    _store(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="portable POSIX path"):
        ingress.submit_skill_create(**_proposal(files={path: "content"}))


def test_creation_bounds_evidence_envelope(tmp_path, monkeypatch):
    from ingot.optimize import ingress

    _store(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="2 to 12"):
        ingress.submit_skill_create(**_proposal(evidence=[str(i) for i in range(13)]))


def test_creation_rejects_unsafe_router_globs_and_unicode_aliases(tmp_path, monkeypatch):
    from ingot.optimize import ingress

    _store(tmp_path, monkeypatch)
    frontmatter = {"metadata": {"skill-router": {
        "scopes": ["project"], "path_patterns": ["/tmp/*"]}}}
    with pytest.raises(ValueError, match="relative POSIX globs"):
        ingress.submit_skill_create(**_proposal(frontmatter=frontmatter))
    with pytest.raises(ValueError, match="NFC-normalized"):
        ingress.submit_skill_create(**_proposal(files={"references/e\u0301.md": "content"}))
