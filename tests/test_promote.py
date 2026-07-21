import json
import os
from pathlib import Path

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
    with pytest.raises(ValueError, match="evidence gate blocked"):
        P.approve_pending("pdf")
    assert "old body" in (skill / "SKILL.md").read_text()


def test_promote_refuses_stale_champion_revision(tmp_path, monkeypatch):
    skill = _library(tmp_path, monkeypatch)
    pending = _pending(skill)
    pending["evidence"]["champion"]["revision"] = "stale"
    P.save_pending("pdf", pending)
    with pytest.raises(ValueError, match="champion revision changed"):
        P.approve_pending("pdf")


def test_promote_refuses_bundled_file_drift(tmp_path, monkeypatch):
    skill = _library(tmp_path, monkeypatch)
    (skill / "reference.md").write_text("version one")
    pending = _pending(skill)
    (skill / "reference.md").write_text("version two")
    P.save_pending("pdf", pending)
    with pytest.raises(ValueError, match="champion revision changed"):
        P.approve_pending("pdf")


def test_promote_snapshots_previous_revision_and_swaps_challenger(tmp_path, monkeypatch):
    skill = _library(tmp_path, monkeypatch)
    pending = _pending(skill)
    old_revision = pending["evidence"]["champion"]["revision"]
    P.save_pending("pdf", pending)
    result = P.approve_pending("pdf")
    assert "new body" in (skill / "SKILL.md").read_text()
    assert "old body" in (P.REVISIONS_DIR / "pdf" / old_revision / "SKILL.md").read_text()
    assert not P.pending_path("pdf").exists()
    assert old_revision in result
    assert load_skills(skill.parent)[0].revision == pending["evidence"]["challenger"]["revision"]
    audit = json.loads((tmp_path / "approval-audit.jsonl").read_text())
    assert audit["action"] == "approve" and audit["skill"] == "pdf"

    result = P.rollback("pdf", old_revision)
    assert "old body" in (skill / "SKILL.md").read_text()
    assert "Rolled back" in result
    records = [json.loads(line) for line in (tmp_path / "approval-audit.jsonl").read_text().splitlines()]
    assert [record["action"] for record in records] == ["approve", "rollback"]


def test_approval_succeeds_when_audit_write_fails(tmp_path, monkeypatch, caplog):
    skill = _library(tmp_path, monkeypatch)
    pending = _pending(skill)
    P.save_pending("pdf", pending)
    monkeypatch.setattr(P, "_audit", lambda *args: (_ for _ in ()).throw(OSError("disk full")))

    result = P.approve_pending("pdf")

    assert "Promoted 'pdf'" in result
    assert "new body" in (skill / "SKILL.md").read_text()
    assert not P.pending_path("pdf").exists()
    assert "audit write failed" in caplog.text


def test_rollback_succeeds_when_audit_write_fails(tmp_path, monkeypatch, caplog):
    skill = _library(tmp_path, monkeypatch)
    pending = _pending(skill)
    old_revision = pending["evidence"]["champion"]["revision"]
    P.save_pending("pdf", pending)
    P.approve_pending("pdf")
    monkeypatch.setattr(P, "_audit", lambda *args: (_ for _ in ()).throw(OSError("disk full")))

    result = P.rollback("pdf", old_revision)

    assert "Rolled back" in result
    assert "old body" in (skill / "SKILL.md").read_text()
    assert "audit write failed" in caplog.text


def test_failed_stage_write_leaves_live_skill_untouched(tmp_path, monkeypatch):
    skill = _library(tmp_path, monkeypatch)
    P.save_pending("pdf", _pending(skill))

    def fail_write(*args, **kwargs):
        raise RuntimeError("stage failed")

    monkeypatch.setattr(P, "write_components", fail_write)
    with pytest.raises(RuntimeError, match="stage failed"):
        P.approve_pending("pdf")
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


# --- staging directories: shadowing, cleanup, atomicity ---------------------------------------

def test_promotion_sweeps_a_leftover_staging_directory(tmp_path, monkeypatch):
    """A run killed mid-swap leaves `.pdf.<hex>.stage` in the library root. The registry ignores
    it, and the next promotion clears it rather than accumulating shadows."""
    skill = _library(tmp_path, monkeypatch)
    stale = skill.with_name(f".{skill.name}.deadbeef.stage")
    stale.mkdir()
    (stale / "SKILL.md").write_text("---\nname: pdf\ndescription: Shadow.\n---\nshadow body\n")

    assert load_skills(skill.parent)[0].body == "old body"  # never shadowed the live skill

    P.save_pending("pdf", _pending(skill))
    P.approve_pending("pdf")

    assert not stale.exists()
    assert list(skill.parent.glob(".pdf.*")) == []
    assert load_skills(skill.parent)[0].body == "new body"


def test_sweep_keeps_a_previous_directory_when_the_live_skill_is_missing(tmp_path, monkeypatch):
    """A crash between the two renames leaves the skill's only copy in `.previous`. Deleting that
    would destroy it, so the sweep leaves it alone while the live directory is absent."""
    skill = _library(tmp_path, monkeypatch)
    orphan = skill.with_name(f".{skill.name}.deadbeef.previous")
    skill.rename(orphan)

    P._sweep_staging(skill)

    assert orphan.exists()
    assert (orphan / "SKILL.md").read_text().endswith("old body\n")


def test_sweep_discards_reproducible_copies_even_without_a_live_skill(tmp_path, monkeypatch):
    skill = _library(tmp_path, monkeypatch)
    copies = [skill.with_name(f".{skill.name}.dead{i}.{suffix}")
              for i, suffix in enumerate(("stage", "rollback"))]
    for copy in copies:
        copy.mkdir()
    skill.rename(skill.with_name(".pdf.parked.previous"))

    P._sweep_staging(skill)

    assert not any(copy.exists() for copy in copies)


def test_sweep_ignores_unrelated_and_symlinked_hidden_directories(tmp_path, monkeypatch):
    skill = _library(tmp_path, monkeypatch)
    keep = skill.with_name(".pdf-notes")            # not a staging name
    keep.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete")
    link = skill.with_name(f".{skill.name}.deadbeef.stage")
    link.symlink_to(outside, target_is_directory=True)

    P._sweep_staging(skill)

    assert keep.exists()
    assert (outside / "keep.txt").exists()


def test_failed_rollback_copy_leaves_no_partial_staging_directory(tmp_path, monkeypatch):
    skill = _library(tmp_path, monkeypatch)
    P.save_pending("pdf", _pending(skill))
    old_revision = P.load_pending("pdf")["evidence"]["champion"]["revision"]
    P.approve_pending("pdf")

    def fail_copy(src, dst, **kwargs):
        Path(dst).mkdir(parents=True, exist_ok=True)   # a partially copied tree
        raise RuntimeError("copy failed")

    monkeypatch.setattr(P.shutil, "copytree", fail_copy)
    with pytest.raises(RuntimeError, match="copy failed"):
        P.rollback("pdf", old_revision)

    assert list(skill.parent.glob(".pdf.*")) == []
    assert "new body" in (skill / "SKILL.md").read_text()   # the live skill is untouched


def test_failed_snapshot_copy_leaves_no_partial_snapshot(tmp_path, monkeypatch):
    skill = _library(tmp_path, monkeypatch)
    P.save_pending("pdf", _pending(skill))

    def fail_copy(src, dst, **kwargs):
        Path(dst).mkdir(parents=True, exist_ok=True)
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(P.shutil, "copytree", fail_copy)
    with pytest.raises(RuntimeError, match="snapshot failed"):
        P.approve_pending("pdf")

    assert list((P.REVISIONS_DIR / "pdf").glob("*")) == []
    assert "old body" in (skill / "SKILL.md").read_text()


# --- snapshot ordering ------------------------------------------------------------------------

def _promote_body(skill, body):
    """Promote one new body through the real gate and return the revision it displaced."""
    champion = optimizable_components(skill)
    challenger = {**champion, "body": body}
    current = load_skills(skill.parent)[0]
    gate = {"promotable": True, "blocked": []}
    P.save_pending("pdf", {
        "skill": "pdf", "champion_components": champion, "challenger_components": challenger,
        "gate": gate,
        "evidence": {"champion": {"revision": current.revision},
                     "challenger": {"revision": skill_revision(skill, challenger)}, "gate": gate},
    })
    P.approve_pending("pdf")
    return current.revision


def test_rollback_then_promote_orders_the_restored_revision_first(tmp_path, monkeypatch):
    """copytree copies the source directory's timestamps onto the snapshot, and re-snapshotting an
    existing revision copies nothing at all, so directory mtime cannot order rollback targets."""
    skill = _library(tmp_path, monkeypatch)
    first = _promote_body(skill, "second body")     # snapshot A (the original)
    second = _promote_body(skill, "third body")     # snapshot B

    assert [r["revision"] for r in P.list_revisions("pdf")] == [second, first]

    P.rollback("pdf", first)                        # snapshot C (third body), live is back on A
    third = [r["revision"] for r in P.list_revisions("pdf")][0]
    assert third not in (first, second)

    _promote_body(skill, "fourth body")             # re-displaces A, which is already stored

    order = [r["revision"] for r in P.list_revisions("pdf")]
    assert order[0] == first, "the revision the last promotion displaced must be the newest target"
    assert set(order) == {first, second, third}
    assert all(entry["created"] > 0 for entry in P.list_revisions("pdf"))


def test_list_revisions_falls_back_to_mtime_without_an_index(tmp_path, monkeypatch):
    """Snapshots taken before the index existed still list, ordered below stamped ones."""
    _library(tmp_path, monkeypatch)
    legacy = P.REVISIONS_DIR / "pdf" / ("a" * 8)
    legacy.mkdir(parents=True)
    assert [r["revision"] for r in P.list_revisions("pdf")] == ["a" * 8]
    assert P.list_revisions("pdf")[0]["created"] > 0


def _write_snapshot_index(text: str) -> None:
    path = P.snapshot_index_path("pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _snapshot_dir(name: str) -> None:
    (P.REVISIONS_DIR / "pdf" / name).mkdir(parents=True, exist_ok=True)


@pytest.mark.parametrize("index", [
    '{"aaaaaaaa": {"seq": "newest", "created": "yesterday"}}',   # strings where numbers belong
    '{"aaaaaaaa": "not a record"}',                              # a value that is not an entry
    '{"aaaaaaaa": {"seq": [3], "created": null}}',               # a list and a null
    '{"aaaaaaaa": {"seq": true, "created": true}}',              # booleans are not sequence numbers
    '{"aaaaaaaa": {"seq": 1}, ',                                 # truncated JSON
    '["aaaaaaaa"]',                                              # a list, not an index
])
def test_list_revisions_tolerates_a_malformed_index(tmp_path, monkeypatch, index):
    """The index is hand-editable JSON. A value of the wrong type must degrade the ordering, not
    raise out of the history view that the fallback exists to keep rendering."""
    _library(tmp_path, monkeypatch)
    _snapshot_dir("a" * 8)
    _write_snapshot_index(index)

    listed = P.list_revisions("pdf")

    assert [r["revision"] for r in listed] == ["a" * 8]
    assert listed[0]["created"] > 0          # fell back to directory mtime
    assert set(listed[0]) == {"revision", "created"}


def test_stamping_recovers_from_a_malformed_index_and_still_orders_the_newest_first(tmp_path,
                                                                                    monkeypatch):
    """A corrupt entry must not block the next stamp: the promotion that follows it has to be
    recorded, and its snapshot has to sort ahead of the entries that could not be read."""
    skill = _library(tmp_path, monkeypatch)
    _snapshot_dir("a" * 8)
    _write_snapshot_index('{"aaaaaaaa": {"seq": "newest", "created": "yesterday"}}')

    displaced = _promote_body(skill, "second body")

    index = json.loads(P.snapshot_index_path("pdf").read_text())
    assert index[displaced]["seq"] == 1      # the unreadable entry counted as 0
    assert [r["revision"] for r in P.list_revisions("pdf")] == [displaced, "a" * 8]


@pytest.mark.parametrize("stamp_error", [TypeError("unorderable index"), OSError("read-only store")])
def test_promotion_survives_a_stamp_failure(tmp_path, monkeypatch, caplog, stamp_error):
    """Stamping is best effort for every failure, a corrupt/unorderable index or an unwritable store
    alike: a promotion that already swapped the directory must not be lost to whatever makes the
    stamp raise."""
    skill = _library(tmp_path, monkeypatch)
    P.save_pending("pdf", _pending(skill))
    monkeypatch.setattr(P, "_stamp_snapshot",
                        lambda *a: (_ for _ in ()).throw(stamp_error))

    assert "Promoted 'pdf'" in P.approve_pending("pdf")
    assert "new body" in (skill / "SKILL.md").read_text()   # the directory swap still happened
    assert "snapshot index write failed" in caplog.text
    assert [r["revision"] for r in P.list_revisions("pdf")]   # mtime fallback still lists it


def test_snapshot_index_is_not_restored_into_the_live_skill(tmp_path, monkeypatch):
    skill = _library(tmp_path, monkeypatch)
    first = _promote_body(skill, "second body")

    P.rollback("pdf", first)

    assert P.snapshot_index_path("pdf").exists()
    assert not (skill / ".snapshots.json").exists()
    assert sorted(p.name for p in skill.iterdir()) == ["SKILL.md"]


# --- approval trail ---------------------------------------------------------------------------

def test_read_audit_tolerates_non_utf8_and_malformed_lines(tmp_path, monkeypatch):
    _library(tmp_path, monkeypatch)
    trail = P.audit_path()
    trail.parent.mkdir(parents=True, exist_ok=True)
    trail.write_bytes(
        b'{"action":"approve","skill":"pdf"}\n'
        b'\xff\xfe not utf-8 at all\n'
        b'{"action":"rollback","skill":"pdf"}\n'
        b'{"truncated":\n'
        b'"a bare string, not a record"\n'
    )

    page = P.read_audit()

    assert [r["action"] for r in page["records"]] == ["rollback", "approve"]
    assert page["total"] == 2


def test_read_audit_reports_the_total_beyond_the_page_limit(tmp_path, monkeypatch):
    _library(tmp_path, monkeypatch)
    trail = P.audit_path()
    trail.parent.mkdir(parents=True, exist_ok=True)
    trail.write_text("".join(json.dumps({"action": "approve", "n": i}) + "\n" for i in range(60)))

    page = P.read_audit(limit=50)

    assert len(page["records"]) == 50 and page["total"] == 60
    assert page["records"][0]["n"] == 59        # newest first


def test_audit_append_completes_a_short_write(tmp_path, monkeypatch):
    """os.write may write fewer bytes than asked; a half-written record would be unparseable."""
    _library(tmp_path, monkeypatch)
    real_write = os.write

    def short_write(fd, data):
        return real_write(fd, data[:1])          # one byte per call

    monkeypatch.setattr(P.os, "write", short_write)
    P._audit("approve", "pdf", "abc123")

    records = [json.loads(line) for line in P.audit_path().read_text().splitlines()]
    assert records == [{"schema_version": 1, "ts": records[0]["ts"], "action": "approve",
                        "skill": "pdf", "revision": "abc123", "actor": "local-operator"}]
