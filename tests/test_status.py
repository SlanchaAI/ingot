"""`ingot status`: the four states, decided by observation rather than by a configuration flag.

MANAGED, PENDING, DRIFTED, UNMANAGED are answers about what is actually served compared with what
the last successful release receipt says should be served. A status command that read its verdict
back out of the configuration would agree with the claim instead of testing it."""
import pytest

from ingot import cli, status
from ingot.optimize import promote as P
from ingot.optimize import publication as Q


def _library(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv("INGOT_LIBRARY", str(root))
    return root


def _skill(root, name, body="a body"):
    directory = root / name
    directory.mkdir(exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Does the {name} thing.\n---\n{body}\n")
    from ingot.mcp_server.registry import skill_revision
    return skill_revision(directory)


def _release(name, revision, *, champion="absent", state="active"):
    """A receipt for one skill, driven through the real queue rather than hand-authored."""
    receipt = Q.queue_publication(name, {
        "skill": name, "kind": "creation",
        "challenger_components": {"description": f"Does the {name} thing.", "body": "a body"},
        "evidence": {"champion": {"revision": champion}, "challenger": {"revision": revision}},
    }, "admin", "promote")
    if state != "approved_publishing":
        Q.update_publication(receipt.id, state=state)
    return receipt


def test_a_skill_serving_its_released_revision_is_managed(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    _release("pdf", _skill(root, "pdf"))

    result = status.library_status(root)

    assert result["mode"] == status.MANAGED
    assert result["skills"] == [{"skill": "pdf", "state": status.MANAGED,
                                 "revision": result["skills"][0]["revision"],
                                 "released": result["skills"][0]["revision"],
                                 "publication": result["skills"][0]["publication"]}]


def test_an_out_of_band_edit_reports_drifted(tmp_path, monkeypatch):
    """A read-only mount does not stop the machine owner from editing the host directory. This is
    the detection that replaces claiming it does."""
    root = _library(tmp_path, monkeypatch)
    _release("pdf", _skill(root, "pdf"))
    _skill(root, "pdf", body="edited by hand, out of band")

    result = status.library_status(root)

    assert result["mode"] == status.DRIFTED
    assert result["skills"][0]["state"] == status.DRIFTED
    assert result["skills"][0]["revision"] != result["skills"][0]["released"]
    assert "outside the publisher" in status.render(result)


def test_restoring_the_released_bytes_returns_to_managed(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    released = _skill(root, "pdf")
    _release("pdf", released)
    _skill(root, "pdf", body="edited by hand, out of band")
    assert status.library_status(root)["mode"] == status.DRIFTED

    _skill(root, "pdf")

    assert status.library_status(root)["mode"] == status.MANAGED


def test_a_skill_with_no_release_receipt_is_unmanaged_not_drifted(tmp_path, monkeypatch):
    """Fetched, copied, or committed by hand. Real and common, and not drift: there is no release
    for it to have drifted from, and calling it drift would make the alarm meaningless."""
    root = _library(tmp_path, monkeypatch)
    _skill(root, "pdf")

    result = status.library_status(root)

    assert result["mode"] == status.UNMANAGED
    assert result["skills"][0]["state"] == status.UNMANAGED
    assert result["skills"][0]["released"] is None


def test_a_released_skill_with_a_newer_publication_in_flight_is_pending(tmp_path, monkeypatch):
    """Serving the last release while the next one travels is the normal state, not drift."""
    root = _library(tmp_path, monkeypatch)
    released = _skill(root, "pdf")
    _release("pdf", released)
    in_flight = _release("pdf", "c" * 64, champion=released, state="publishing")

    result = status.library_status(root)

    assert Q.load_publication(in_flight.id)["state"] == "publishing"
    assert result["skills"][0]["released"] == released
    assert result["skills"][0]["state"] == status.PENDING
    assert result["mode"] == status.PENDING


def test_a_skill_in_flight_with_no_release_yet_is_pending(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    _release("pdf", _skill(root, "pdf"), state="publishing")

    assert status.library_status(root)["mode"] == status.PENDING


def test_a_creation_in_flight_is_pending_even_though_nothing_serves_it_yet(tmp_path, monkeypatch):
    """Caught in situ: a new skill is served by nothing and released by nothing, so a status built
    from those two sets alone called an empty library fully MANAGED mid-publication."""
    root = _library(tmp_path, monkeypatch)
    _release("csv-tidy", "e" * 64, state="approved_publishing")

    result = status.library_status(root)

    assert result["mode"] == status.PENDING
    assert [entry["skill"] for entry in result["skills"]] == ["csv-tidy"]
    assert result["skills"][0]["revision"] == status.ABSENT


def test_a_quarantined_proposal_is_pending(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    _release("pdf", _skill(root, "pdf"))
    P.save_pending("pdf", {"skill": "pdf", "gate": {"promotable": True},
                           "evidence": {"challenger": {"revision": "b" * 64}}})

    assert status.library_status(root)["mode"] == status.PENDING


def test_drift_outranks_a_publication_in_flight(tmp_path, monkeypatch):
    """The alarm must not be masked by an unrelated change travelling to the vault."""
    root = _library(tmp_path, monkeypatch)
    released = _skill(root, "pdf")
    _release("pdf", released)
    _release("tailwind", _skill(root, "tailwind"))
    _release("tailwind", "d" * 64, champion=released, state="publishing")
    _skill(root, "pdf", body="edited by hand, out of band")

    assert status.library_status(root)["mode"] == status.DRIFTED


def test_an_empty_library_is_managed(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)

    assert status.library_status(root)["mode"] == status.MANAGED


def test_development_mode_is_unmanaged_whatever_the_receipts_say(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    _release("pdf", _skill(root, "pdf"))
    monkeypatch.setenv("INGOT_MODE", "dev")

    result = status.library_status(root)

    assert result["mode"] == status.UNMANAGED
    assert result["development_mode"] is True
    assert "do not apply" in status.render(result)


def test_a_writable_library_is_reported_without_deciding_the_verdict(tmp_path, monkeypatch):
    """The administrator who owns the vault can always write it. A status command that answered
    UNMANAGED from their shell would hide the drift they most need to see."""
    root = _library(tmp_path, monkeypatch)
    _release("pdf", _skill(root, "pdf"))

    result = status.library_status(root)

    assert result["mode"] == status.MANAGED
    assert result["writable_roots"] == [str(root.resolve())]
    assert "without an approval" in status.render(result)


def test_status_exits_zero_only_when_everything_is_as_approved(tmp_path, monkeypatch, capsys):
    root = _library(tmp_path, monkeypatch)
    _release("pdf", _skill(root, "pdf"))
    assert cli.main(["status", "--root", str(root)]) == 0

    _skill(root, "pdf", body="edited by hand, out of band")

    assert cli.main(["status", "--root", str(root)]) == 1
    assert status.DRIFTED in capsys.readouterr().out


def test_the_json_payload_is_versioned(tmp_path, monkeypatch, capsys):
    import json
    root = _library(tmp_path, monkeypatch)
    _skill(root, "pdf")

    cli.main(["status", "--root", str(root), "--json"])

    assert json.loads(capsys.readouterr().out)["schema_version"] == status.STATUS_SCHEMA


# --------------------------------------------------------------------------- delivery targets

def _delivery(tmp_path, monkeypatch, vault):
    """One native filesystem target beside the managed vault, configured the way an operator would."""
    native = tmp_path / "claude"
    native.mkdir()
    monkeypatch.setenv("INGOT_VAULT_PATH", str(vault))
    monkeypatch.setenv("INGOT_DELIVERY_TARGETS", f"claude=filesystem:{native}")
    return native


def _copy(source, destination):
    import shutil
    shutil.copytree(source, destination, dirs_exist_ok=True)


def test_a_target_holding_the_released_revision_is_managed(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    native = _delivery(tmp_path, monkeypatch, root)
    _release("pdf", _skill(root, "pdf"))
    _copy(root / "pdf", native / "pdf")

    targets = status.target_states()

    assert [(entry["name"], entry["state"]) for entry in targets] == [
        ("vault", status.MANAGED), ("claude", status.MANAGED)]


def test_only_the_target_that_was_altered_reports_drift(tmp_path, monkeypatch):
    """The acceptance case. Editing a native skill root must not implicate the vault, and the vault
    still holding the release must not hide the edit."""
    root = _library(tmp_path, monkeypatch)
    native = _delivery(tmp_path, monkeypatch, root)
    _release("pdf", _skill(root, "pdf"))
    _copy(root / "pdf", native / "pdf")

    (native / "pdf" / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: Does the pdf thing.\n---\nedited out of band\n")

    states = {entry["name"]: entry["state"] for entry in status.target_states()}
    assert states == {"vault": status.MANAGED, "claude": status.DRIFTED}


def test_a_released_skill_deleted_from_a_target_is_drift_not_silence(tmp_path, monkeypatch):
    root = _library(tmp_path, monkeypatch)
    native = _delivery(tmp_path, monkeypatch, root)
    _release("pdf", _skill(root, "pdf"))

    states = {entry["name"]: entry["state"] for entry in status.target_states()}
    assert states["claude"] == status.DRIFTED


def test_a_target_is_judged_only_on_the_skills_ingot_released_there(tmp_path, monkeypatch):
    """A native skill root is shared. Skills the operator put there themselves are not Ingot's to
    grade, and reporting them would make every real deployment permanently UNMANAGED."""
    root = _library(tmp_path, monkeypatch)
    native = _delivery(tmp_path, monkeypatch, root)
    _release("pdf", _skill(root, "pdf"))
    _copy(root / "pdf", native / "pdf")
    _skill(native, "somebody-elses-skill")

    claude = [entry for entry in status.target_states() if entry["name"] == "claude"][0]

    assert claude["state"] == status.MANAGED
    assert [skill["skill"] for skill in claude["skills"]] == ["pdf"]


def test_a_drifted_target_is_not_hidden_by_a_clean_vault(tmp_path, monkeypatch, capsys):
    """`ingot status` answering MANAGED while a native skill root serves the wrong bytes is exactly
    the lie this command exists to prevent."""
    root = _library(tmp_path, monkeypatch)
    native = _delivery(tmp_path, monkeypatch, root)
    _release("pdf", _skill(root, "pdf"))
    _copy(root / "pdf", native / "pdf")
    (native / "pdf" / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: Does the pdf thing.\n---\nedited out of band\n")

    assert cli.main(["status", "--root", str(root)]) == 1

    output = capsys.readouterr().out
    assert output.startswith(status.DRIFTED)
    assert "claude" in output and str(native) in output


def test_an_unusable_delivery_configuration_is_reported_rather_than_raised(tmp_path, monkeypatch,
                                                                          capsys):
    """Status is the command an operator runs when something is wrong. It has to survive a bad
    environment variable and say what is wrong with it."""
    root = _library(tmp_path, monkeypatch)
    monkeypatch.setenv("INGOT_VAULT_PATH", str(root))
    monkeypatch.setenv("INGOT_DELIVERY_TARGETS", "claude=carrier-pigeon:/tmp/x")

    assert cli.main(["status", "--root", str(root)]) == 1

    assert "unknown delivery kind" in capsys.readouterr().out
