"""Delivery targets: the approved revision reaching more than one place, without a second writer.

Everything here guards the same boundary. A delivery target is somewhere the publisher installs an
approved revision *after* the vault already carries it -- a native agent's skill root, say. It is
not a second authority: it never decides what is approved, it cannot activate anything, and a
target the publisher fails to write must not leave the release looking finished. The tests below
are the difference between that and a `cp` in a cron job."""
import json
import os
import stat

import pytest

from ingot import delivery
from ingot.mcp_server.registry import skill_revision
from ingot.optimize import promote

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return root


def _package(root, name="pdf", body="Combine PDFs.", asset=PNG):
    """A skill directory with a binary asset and an executable, the two things a text-only copy
    would quietly damage."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Merge and split PDF files.\n---\n\n{body}\n",
        encoding="utf-8")
    if asset is not None:
        (directory / "assets").mkdir(exist_ok=True)
        (directory / "assets" / "logo.png").write_bytes(asset)
    runner = directory / "run.sh"
    runner.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    runner.chmod(0o755)
    return directory


# --- configuration ------------------------------------------------------------------------------

def test_an_unconfigured_deployment_delivers_to_the_vault_and_nowhere_else(vault):
    targets = delivery.load_targets({}, vault=vault)
    assert [(target.name, target.kind, target.root) for target in targets] == [
        ("vault", delivery.MANAGED_MCP, vault)]


def test_a_filesystem_target_joins_the_vault_rather_than_replacing_it(tmp_path, vault):
    native = tmp_path / "claude" / "skills"
    targets = delivery.load_targets(
        {delivery.TARGETS: f"vault=managed-mcp:{vault},claude=filesystem:{native}"}, vault=vault)
    assert [target.name for target in targets] == ["vault", "claude"]
    assert targets[1].kind == delivery.FILESYSTEM
    assert targets[1].root == native


def test_the_vault_is_delivered_to_even_when_the_configuration_forgets_it(tmp_path, vault):
    """Dropping the managed target from the list must not stop serving MCP. The vault is where
    publication authority lives; a delivery list is not the place to switch it off."""
    native = tmp_path / "native"
    targets = delivery.load_targets({delivery.TARGETS: f"claude=filesystem:{native}"}, vault=vault)
    assert [(target.name, target.kind) for target in targets] == [
        ("vault", delivery.MANAGED_MCP), ("claude", delivery.FILESYSTEM)]


@pytest.mark.parametrize("spec, reason", [
    ("claude=carrier-pigeon:/tmp/x", "unknown delivery kind"),
    ("=filesystem:/tmp/x", "invalid delivery target name"),
    ("Claude Skills=filesystem:/tmp/x", "invalid delivery target name"),
    ("claude=filesystem:relative/path", "must be an absolute path"),
    ("claude=filesystem:/tmp/x,claude=filesystem:/tmp/y", "duplicate delivery target"),
    ("a=filesystem:/tmp/x,b=filesystem:/tmp/x", "same directory"),
    ("claude=filesystem", "expected name=kind:path"),
])
def test_an_unusable_target_is_refused_at_configuration_time(spec, reason, vault):
    """The publisher validates its configuration before it will start. A delivery target that
    cannot work has to fail there, not on the first approval that tries to use it."""
    with pytest.raises(ValueError, match=reason):
        delivery.load_targets({delivery.TARGETS: spec}, vault=vault)


def test_a_managed_target_that_is_not_the_vault_is_refused(tmp_path, vault):
    with pytest.raises(ValueError, match="managed-mcp target must be the vault"):
        delivery.load_targets({delivery.TARGETS: f"x=managed-mcp:{tmp_path / 'elsewhere'}"},
                              vault=vault)


def test_a_filesystem_target_inside_the_vault_is_refused(vault):
    """It would write into the checkout the publisher just committed, so the vault would drift from
    its own release the moment delivery finished."""
    with pytest.raises(ValueError, match="inside the vault"):
        delivery.load_targets({delivery.TARGETS: f"x=filesystem:{vault / 'nested'}"}, vault=vault)


# --- installing ---------------------------------------------------------------------------------

def test_the_delivered_target_is_byte_identical_to_the_source(tmp_path, vault):
    source = _package(vault)
    native = tmp_path / "native"
    target = delivery.Target("claude", delivery.FILESYSTEM, native)

    delivery.install(target, "pdf", source, skill_revision(source))

    delivered = native / "pdf"
    assert (delivered / "assets" / "logo.png").read_bytes() == PNG
    assert (delivered / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
    assert skill_revision(delivered) == skill_revision(source)


def test_the_executable_bit_survives_delivery(tmp_path, vault):
    source = _package(vault)
    target = delivery.Target("claude", delivery.FILESYSTEM, tmp_path / "native")

    delivery.install(target, "pdf", source, skill_revision(source))

    mode = (tmp_path / "native" / "pdf" / "run.sh").stat().st_mode
    assert mode & stat.S_IXUSR


def test_delivering_a_revision_the_target_already_holds_changes_nothing(tmp_path, vault):
    source = _package(vault)
    target = delivery.Target("claude", delivery.FILESYSTEM, tmp_path / "native")
    revision = skill_revision(source)
    delivery.install(target, "pdf", source, revision)
    before = (tmp_path / "native" / "pdf").stat().st_mtime_ns

    delivery.install(target, "pdf", source, revision)

    assert (tmp_path / "native" / "pdf").stat().st_mtime_ns == before


def test_the_displaced_target_is_snapshotted_before_it_is_replaced(tmp_path, vault):
    """Whatever was there is recoverable, including bytes that never came from a release: a target
    someone edited by hand is exactly the case where the previous content matters most."""
    target = delivery.Target("claude", delivery.FILESYSTEM, tmp_path / "native")
    displaced = _package(tmp_path / "old", body="The bytes that were there.")
    delivery.install(target, "pdf", displaced, skill_revision(displaced))
    displaced_revision = skill_revision(tmp_path / "native" / "pdf")

    replacement = _package(vault, body="The approved bytes.")
    delivery.install(target, "pdf", replacement, skill_revision(replacement))

    stored = promote.revisions_dir() / "pdf" / displaced_revision
    assert (stored / "SKILL.md").read_text(encoding="utf-8").endswith("The bytes that were there.\n")


def test_a_delivery_that_cannot_finish_leaves_the_previous_revision_in_place(tmp_path, vault,
                                                                            monkeypatch):
    """The half-written target is the failure this exists to prevent: an agent loading a skill
    directory that is neither the old revision nor the new one."""
    target = delivery.Target("claude", delivery.FILESYSTEM, tmp_path / "native")
    original = _package(tmp_path / "old", body="The bytes that were there.")
    delivery.install(target, "pdf", original, skill_revision(original))
    original_revision = skill_revision(tmp_path / "native" / "pdf")

    replacement = _package(vault, body="The approved bytes.")
    monkeypatch.setattr(delivery.os, "replace", _explode)
    with pytest.raises(OSError):
        delivery.install(target, "pdf", replacement, skill_revision(replacement))

    assert skill_revision(tmp_path / "native" / "pdf") == original_revision
    assert [entry for entry in (tmp_path / "native").iterdir() if entry.name != "pdf"] == []


def _explode(*args, **kwargs):
    raise OSError("no space left on device")


def test_a_delivery_that_fails_after_displacing_the_target_puts_it_back(tmp_path, vault,
                                                                       monkeypatch):
    """The dangerous window. Replacing a directory takes two renames, and a failure between them
    leaves the destination missing unless the displaced copy is moved back."""
    target = delivery.Target("claude", delivery.FILESYSTEM, tmp_path / "native")
    original = _package(tmp_path / "old", body="The bytes that were there.")
    delivery.install(target, "pdf", original, skill_revision(original))
    original_revision = skill_revision(tmp_path / "native" / "pdf")

    real_replace = os.replace
    restored = []

    def fail_installing_the_staged_copy(source, destination, *args, **kwargs):
        # Precisely the second rename of the swap: the displaced original is already out of the
        # way and the staged copy is going in. Counting calls would catch the snapshot index write
        # instead, which is not the window being tested.
        if str(source).endswith(".tmp"):
            raise OSError("no space left on device")
        if str(source).endswith(".old"):
            restored.append(destination)
        return real_replace(source, destination, *args, **kwargs)

    replacement = _package(vault, body="The approved bytes.")
    monkeypatch.setattr(delivery.os, "replace", fail_installing_the_staged_copy)
    with pytest.raises(OSError):
        delivery.install(target, "pdf", replacement, skill_revision(replacement))

    assert restored == [tmp_path / "native" / "pdf"], "the displaced directory was never moved back"
    assert skill_revision(tmp_path / "native" / "pdf") == original_revision
    assert sorted(entry.name for entry in (tmp_path / "native").iterdir()) == ["pdf"]


def test_delivering_bytes_that_do_not_match_the_receipt_is_refused(tmp_path, vault):
    """The check is against the revision the receipt names, not against the source directory, so a
    source that was tampered with between approval and delivery cannot install itself."""
    source = _package(vault)
    target = delivery.Target("claude", delivery.FILESYSTEM, tmp_path / "native")

    with pytest.raises(ValueError, match="does not match the approved revision"):
        delivery.install(target, "pdf", source, "0" * 16)

    assert not (tmp_path / "native" / "pdf").exists()


def test_a_symlink_in_the_source_is_refused_rather_than_followed(tmp_path, vault):
    """Following it would copy whatever it points at -- possibly from outside the vault entirely --
    into an agent's skill root as an ordinary file."""
    source = _package(vault)
    # A link pointing *out* of the skill is already refused upstream: `skill_revision` will not
    # hash one. A contained link is the case that reaches here, and copying it without `symlinks`
    # silently turns it into a second regular file holding the same bytes.
    (source / "readme.md").symlink_to(source / "SKILL.md")
    target = delivery.Target("claude", delivery.FILESYSTEM, tmp_path / "native")

    with pytest.raises(ValueError, match="symlink-unsupported"):
        delivery.install(target, "pdf", source, skill_revision(source))

    assert not (tmp_path / "native" / "pdf").exists()


def test_delivering_an_absence_removes_the_skill_and_snapshots_it(tmp_path, vault):
    target = delivery.Target("claude", delivery.FILESYSTEM, tmp_path / "native")
    source = _package(vault)
    delivery.install(target, "pdf", source, skill_revision(source))
    revision = skill_revision(source)

    delivery.install(target, "pdf", None, promote.ABSENT_REVISION)

    assert not (tmp_path / "native" / "pdf").exists()
    assert (promote.revisions_dir() / "pdf" / revision).is_dir()


def test_removing_a_skill_the_target_never_held_is_not_an_error(tmp_path, vault):
    target = delivery.Target("claude", delivery.FILESYSTEM, tmp_path / "native")
    delivery.install(target, "pdf", None, promote.ABSENT_REVISION)
    assert not (tmp_path / "native" / "pdf").exists()


def test_delivery_never_writes_a_managed_target(tmp_path, vault):
    """The vault is written by the publication commit and the fast-forward, and by nothing else.
    A second writer there is the invariant this whole control plane exists to hold."""
    _package(vault, body="What the vault holds.")
    # Deliberately a *different* revision. Asking the managed target to install bytes it does not
    # hold is the only way to tell the refusal apart from delivery being a no-op by luck.
    elsewhere = _package(tmp_path / "other", body="Something else entirely.")
    target = delivery.Target("vault", delivery.MANAGED_MCP, vault)
    before = skill_revision(vault / "pdf")

    assert delivery.install(target, "pdf", elsewhere, skill_revision(elsewhere)) is False

    assert skill_revision(vault / "pdf") == before
    assert sorted(path.name for path in vault.iterdir()) == ["pdf"]


# --- observing ----------------------------------------------------------------------------------

def test_a_target_reports_the_revision_it_actually_holds(tmp_path, vault):
    source = _package(vault)
    target = delivery.Target("claude", delivery.FILESYSTEM, tmp_path / "native")
    delivery.install(target, "pdf", source, skill_revision(source))

    assert delivery.observed(target, "pdf") == skill_revision(source)


def test_a_target_edited_out_of_band_reports_the_new_bytes_not_the_old_ones(tmp_path, vault):
    source = _package(vault)
    target = delivery.Target("claude", delivery.FILESYSTEM, tmp_path / "native")
    delivery.install(target, "pdf", source, skill_revision(source))

    (tmp_path / "native" / "pdf" / "assets" / "logo.png").write_bytes(PNG + b"tampered")

    assert delivery.observed(target, "pdf") != skill_revision(source)


def test_a_missing_skill_reads_as_absent_rather_than_as_an_error(tmp_path, vault):
    target = delivery.Target("claude", delivery.FILESYSTEM, tmp_path / "native")
    assert delivery.observed(target, "pdf") == promote.ABSENT_REVISION
