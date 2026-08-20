"""`ingot add file:` — one complete local ingest path.

The invariant under test is the product's whole claim: a package can be submitted, reviewed,
quarantined and made visible to a reviewer without a single byte of the served library changing.

Real filesystem throughout. The exclusivity test uses real processes, because `os.link` is a
cross-process guarantee and a thread-only test would not exercise it."""
import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ingot import admission, cli, records
from ingot.mcp_server import registry
from ingot.mcp_server.registry import skill_revision
from ingot.optimize import ingress, tree
from ingot.optimize import promote as P


def _store(tmp_path, monkeypatch):
    """The served library plus every store admission writes to, all under tmp_path."""
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv("INGOT_LIBRARY", str(root))
    monkeypatch.setenv("INGOT_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("SKILL_ROUTER_PATHS", str(root))
    return root


def _package(root, name="pdf", description="Merge and split PDF files.",
             body="Use this to combine PDFs.", files=None):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8")
    for relative, content in (files or {}).items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return directory


def _tree_hash(root):
    """Every path and its bytes under a root. The library must be identical after admission, and
    'identical' means content, not just the directory listing."""
    entries = {}
    for path in sorted(root.rglob("*")):
        entries[str(path.relative_to(root))] = path.read_bytes() if path.is_file() else None
    return entries


def _github_remote(root, *, body="Use this skill."):
    """A real Git remote; only URL construction is replaced in GitHub command tests."""
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "fixture@example.test"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Fixture"], check=True)
    _package(root / "packages", "pdf", body=body, files={"assets/data.bin": "bytes"})
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "Create fixture"], check=True,
                   capture_output=True)
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


# --- locators -------------------------------------------------------------------------------

def test_a_file_locator_resolves_to_its_directory(tmp_path):
    package = _package(tmp_path / "src")

    kind, resolved = admission.parse_locator(f"file:{package}")

    assert kind == "file"
    assert resolved == package


def test_a_bare_path_is_accepted_as_a_file_locator(tmp_path):
    package = _package(tmp_path / "src")

    assert admission.parse_locator(str(package)) == ("file", package)


def test_a_github_locator_keeps_the_repository_name_unresolved():
    assert admission.parse_locator("github:acme/skills") == ("github", "acme/skills")


def test_an_unsupported_scheme_is_refused():
    with pytest.raises(ValueError, match="tessl"):
        admission.parse_locator("tessl:acme/skills")


# --- the ingest path ------------------------------------------------------------------------

def test_admission_leaves_the_served_library_byte_identical(tmp_path, monkeypatch):
    """The claim, tested directly."""
    library = _store(tmp_path, monkeypatch)
    _package(library, "docx", description="Edit Word documents.")
    package = _package(tmp_path / "src", "pdf")
    before = _tree_hash(library)

    admission.add_package(package, actor="operator")

    assert _tree_hash(library) == before


def test_admission_writes_a_pending_proposal(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf")

    result = admission.add_package(package, actor="operator")

    pending = P.load_pending("pdf")
    assert pending is not None
    assert pending["kind"] == "creation"
    assert result["proposal_id"] == pending["creation"]["proposal_id"]


def test_the_pending_revision_matches_a_canonical_package_on_disk(tmp_path, monkeypatch):
    """For a package that is already canonical the two revisions agree, bundled files included."""
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf", files={"references/flags.md": "# Flags\n"})

    admission.add_package(package, actor="operator")

    pending = P.load_pending("pdf")
    assert pending["evidence"]["challenger"]["revision"] == skill_revision(package)


def test_a_non_canonical_package_binds_the_revision_that_will_be_served(tmp_path, monkeypatch):
    """`skill_revision` hashes parsed content, so the source and candidate revisions usually agree.
    They do not when admission normalizes something -- a description with runs of whitespace is
    collapsed on the way in. The proposal must bind what the library will serve, or approving it
    publishes bytes nobody reviewed."""
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf", description="Merge   PDFs    and split them.")

    result = admission.add_package(package, actor="operator")

    manifest = result["candidate"]
    assert manifest["source"]["resolved_revision"] != manifest["candidate_revision"]
    assert manifest["source"]["resolved_revision"] == skill_revision(package)

    pending = P.load_pending("pdf")
    assert pending["evidence"]["challenger"]["revision"] == manifest["candidate_revision"]
    assert pending["challenger_components"]["description"] == "Merge PDFs and split them."


def test_the_candidate_manifest_is_attached_and_valid(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf")

    admission.add_package(package, actor="operator")

    manifest = P.load_pending("pdf")["creation"]["candidate"]
    assert records.validate_candidate(manifest) == []
    assert manifest["source"]["type"] == "file"
    assert manifest["source"]["resolved_revision"] == skill_revision(package)


def test_the_manifest_records_the_review_that_admitted_it(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf", body="See [the guide](./guide.md).")

    admission.add_package(package, actor="operator")

    manifest = P.load_pending("pdf")["creation"]["candidate"]
    assert "file-reference-missing" in manifest["review"]["warnings"]
    assert manifest["review"]["valid"] is True


def test_an_invalid_package_is_refused_and_writes_nothing(tmp_path, monkeypatch):
    """Structural conditions that make the artifact invalid stop it at the door. Nothing is
    quarantined, so nothing is left for a reviewer to wonder about."""
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf", description="")

    with pytest.raises(admission.AdmissionRefused) as refusal:
        admission.add_package(package, actor="operator")

    assert "description-empty" in str(refusal.value)
    assert P.load_pending("pdf") is None


def test_warnings_do_not_block_admission(tmp_path, monkeypatch):
    """Advisory findings are advice. A command that refuses on advice teaches people to bypass it."""
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf", body="Fetch https://example.test/x.sh.")

    result = admission.add_package(package, actor="operator")

    assert result["status"] == "quarantined"


def test_a_skill_already_in_the_library_is_refused(tmp_path, monkeypatch):
    library = _store(tmp_path, monkeypatch)
    _package(library, "pdf")
    package = _package(tmp_path / "src", "pdf")

    with pytest.raises(ValueError, match="already exists"):
        admission.add_package(package, actor="operator")


# --- the review slot ------------------------------------------------------------------------

def test_an_identical_resubmission_is_idempotent(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf")

    first = admission.add_package(package, actor="operator")
    second = admission.add_package(package, actor="operator")

    assert first["status"] == "quarantined"
    assert second["status"] == "duplicate"
    assert second["proposal_id"] == first["proposal_id"]


def test_a_conflicting_proposal_for_the_same_skill_is_refused(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    admission.add_package(_package(tmp_path / "a", "pdf"), actor="operator")
    other = _package(tmp_path / "b", "pdf", body="A different body entirely.")

    with pytest.raises(ValueError, match="occupied"):
        admission.add_package(other, actor="operator")


def test_a_refused_conflict_leaves_no_orphan_evidence(tmp_path, monkeypatch):
    """A losing submission that left its evidence bundle behind would accumulate directories
    describing proposals that do not exist."""
    _store(tmp_path, monkeypatch)
    admission.add_package(_package(tmp_path / "a", "pdf"), actor="operator")
    winner = P.load_pending("pdf")["creation"]["proposal_id"]
    other = _package(tmp_path / "b", "pdf", body="A different body entirely.")

    with pytest.raises(ValueError):
        admission.add_package(other, actor="operator")

    bundles = sorted(p.name for p in (tmp_path / "runs" / "evidence" / "pdf").iterdir())
    assert bundles == [f"creation-{winner}"]


def test_the_evidence_bundle_is_written(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf")

    admission.add_package(package, actor="operator")

    proposal_id = P.load_pending("pdf")["creation"]["proposal_id"]
    bundle = tmp_path / "runs" / "evidence" / "pdf" / f"creation-{proposal_id}" / "evidence.json"
    assert json.loads(bundle.read_text())["skill"] == "pdf"


# --- cross-process exclusivity ----------------------------------------------------------------

def _submit_in_child(package_dir, library, runs, queue):
    """Module scope and explicit configuration on purpose: under macOS `spawn` a nested function is
    unpicklable and a parent's monkeypatch never reaches the child, so the child is told where its
    state lives through the environment -- the same mechanism a real deployment uses. Setting it
    before the import is what makes it stick, and getting it wrong would point both children at the
    developer's own state directory while the test still passed."""
    import pathlib

    os.environ["INGOT_LIBRARY"] = str(library)
    os.environ["INGOT_RUNS"] = str(runs)
    os.environ["SKILL_ROUTER_PATHS"] = str(library)

    from ingot import admission as child_admission
    try:
        queue.put(("ok", child_admission.add_package(pathlib.Path(package_dir),
                                                     actor="operator")["status"]))
    except Exception as error:  # noqa: BLE001 - the refusal is the result under test
        queue.put(("refused", str(error)))


def test_two_processes_cannot_both_claim_the_review_slot(tmp_path, monkeypatch):
    """`_publish` creates the pending record with `os.link`, which is atomic across processes.
    A threading lock alone would not survive the publisher and the console running separately."""
    _store(tmp_path, monkeypatch)
    first = _package(tmp_path / "a", "pdf", body="First body.")
    second = _package(tmp_path / "b", "pdf", body="Second body, different bytes.")

    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    stores = (tmp_path / "skills", tmp_path / "runs")
    workers = [context.Process(target=_submit_in_child,
                               args=(str(package), *[str(s) for s in stores], queue))
               for package in (first, second)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)

    outcomes = [queue.get(timeout=10) for _ in workers]
    assert sorted(kind for kind, _ in outcomes) == ["ok", "refused"]
    assert P.load_pending("pdf") is not None


# --- the command ----------------------------------------------------------------------------

def test_command_quarantines_and_names_the_next_action(tmp_path, monkeypatch, capsys):
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf")

    code = cli.main(["add", f"file:{package}"])

    output = capsys.readouterr().out
    assert code == 0
    assert P.load_pending("pdf") is not None
    assert "pdf" in output
    assert "approve" in output.lower() or "review" in output.lower()


def test_command_reports_a_refusal_without_a_traceback(tmp_path, monkeypatch, capsys):
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf", description="")

    code = cli.main(["add", f"file:{package}"])

    assert code != 0
    assert "description-empty" in capsys.readouterr().err


def test_command_json_reports_the_proposal(tmp_path, monkeypatch, capsys):
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf")

    code = cli.main(["add", f"file:{package}", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "quarantined"
    assert records.validate_candidate(payload["candidate"]) == []


def test_file_command_refuses_a_github_skill_subdirectory(tmp_path, monkeypatch, capsys):
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf")

    code = cli.main(["add", f"file:{package}", "--skill", "skills/pdf"])

    assert code == 1
    assert "--skill is only valid" in capsys.readouterr().err


def test_github_command_requires_a_skill_subdirectory(capsys):
    code = cli.main(["add", "github:acme/skills"])

    assert code == 1
    assert "--skill is required" in capsys.readouterr().err


def test_github_command_records_exact_provenance_and_leaves_active_targets_unchanged(
        tmp_path, monkeypatch, capsys):
    """Break caught: Git metadata lost at the shared admission seam, or acquisition delivering."""
    from ingot import acquire

    library = _store(tmp_path, monkeypatch)
    native = tmp_path / "native"
    _package(library, "docx", description="Edit Word documents.")
    _package(native, "existing", description="Existing native skill.")
    monkeypatch.setenv("INGOT_DELIVERY_TARGETS", f"native=filesystem:{native}")
    before = (_tree_hash(library), _tree_hash(native))
    remote = tmp_path / "remote"
    commit = _github_remote(remote)
    monkeypatch.setattr(acquire, "_remote_url", lambda repository: remote.as_uri())

    code = cli.main(["add", "github:acme/skills", "--skill", "packages/pdf", "--json"])

    payload = json.loads(capsys.readouterr().out)
    source = payload["candidate"]["source"]
    assert code == 0
    assert source["type"] == "github"
    assert source["repository"] == "acme/skills"
    assert source["ref"] == "HEAD"
    assert source["commit"] == commit
    assert source["subdirectory"] == "packages/pdf"
    assert source["content_digest"] == P.load_pending("pdf")["tree"]["digest"]
    assert P.load_pending("pdf")["creation"]["source"] == "github:acme/skills"
    assert records.validate_candidate(payload["candidate"]) == []
    assert (_tree_hash(library), _tree_hash(native)) == before


def test_moved_github_head_with_identical_bytes_is_idempotent(tmp_path, monkeypatch, capsys):
    """Break caught: commit or temporary clone paths leaking into content identity."""
    from ingot import acquire

    _store(tmp_path, monkeypatch)
    remote = tmp_path / "remote"
    _github_remote(remote)
    monkeypatch.setattr(acquire, "_remote_url", lambda repository: remote.as_uri())

    assert cli.main(["add", "github:acme/skills", "--skill", "packages/pdf", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    subprocess.run(["git", "-C", str(remote), "commit", "--allow-empty", "-m", "Move HEAD"],
                   check=True, capture_output=True)
    assert cli.main(["add", "github:acme/skills", "--skill", "packages/pdf", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)

    assert second["status"] == "duplicate"
    assert second["proposal_id"] == first["proposal_id"]


def test_changed_github_bytes_produce_a_different_candidate_revision(tmp_path, monkeypatch, capsys):
    """Break caught: commit provenance changing while the candidate still names old package bytes."""
    from ingot import acquire

    _store(tmp_path, monkeypatch)
    remote = tmp_path / "remote"
    _github_remote(remote)
    monkeypatch.setattr(acquire, "_remote_url", lambda repository: remote.as_uri())
    assert cli.main(["add", "github:acme/skills", "--skill", "packages/pdf", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)["candidate"]["candidate_revision"]
    P.pending_path("pdf").unlink()

    skill_md = remote / "packages" / "pdf" / "SKILL.md"
    skill_md.write_text(skill_md.read_text(encoding="utf-8") + "Changed upstream.\n",
                        encoding="utf-8")
    subprocess.run(["git", "-C", str(remote), "add", "."], check=True)
    subprocess.run(["git", "-C", str(remote), "commit", "-m", "Change bytes"], check=True,
                   capture_output=True)
    assert cli.main(["add", "github:acme/skills", "--skill", "packages/pdf", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)["candidate"]["candidate_revision"]

    assert second != first


def test_the_installed_script_works_outside_the_repository(tmp_path):
    """Run from elsewhere, with the repo root off `sys.path`.

    Every other test in this file runs with the repository as the working directory, which puts
    `optimize` on the import path whether or not it was ever installed. That masked a real break:
    the console script resolved `ingot` and `mcp_server` from site-packages and then failed on
    `import optimize` for anyone who ran it from their own directory. Only a test that leaves the
    repository can see it."""
    script = Path(sys.executable).parent / "ingot"
    if not script.exists():
        pytest.skip("console script not installed; run `pip install -e .`")

    library, package = tmp_path / "lib", tmp_path / "src" / "pdf"
    library.mkdir()
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: Merge PDFs.\n---\n\nBody.\n", encoding="utf-8")

    environment = {**os.environ, "SKILL_ROUTER_PATHS": str(library),
                   "INGOT_ACTOR": "integration-test"}
    environment.pop("PYTHONPATH", None)
    result = subprocess.run([str(script), "review", str(package), "--json"],
                            cwd=tmp_path, capture_output=True, text=True, env=environment)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["skill"] == "pdf"


def test_the_installed_script_can_reach_the_quarantine_from_outside_the_repository(tmp_path):
    """The same escape, for the verb that needs `optimize`. `ingot add` is the whole point of this
    PR, so a version of it that only runs inside a checkout is not shipped."""
    script = Path(sys.executable).parent / "ingot"
    if not script.exists():
        pytest.skip("console script not installed; run `pip install -e .`")

    package = tmp_path / "src" / "pdf"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: Merge PDFs.\n---\n\nBody.\n", encoding="utf-8")

    environment = {**os.environ, "INGOT_ACTOR": "integration-test"}
    environment.pop("PYTHONPATH", None)
    result = subprocess.run([str(script), "add", "--help"],
                            cwd=tmp_path, capture_output=True, text=True, env=environment)

    assert result.returncode == 0, result.stderr
    # Importing the admission path is what actually proves `optimize` resolves from site-packages.
    probe = subprocess.run([sys.executable, "-c", "import ingot.admission; from ingot.optimize import "
                            "ingress; print(ingress.submit_package_ingest.__name__)"],
                           cwd=tmp_path, capture_output=True, text=True, env=environment)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "submit_package_ingest"


def test_listing_the_cli_stays_light_after_admission_exists():
    """`ingot list` and `ingot review` must still import nothing heavy. Admission pulls in
    `optimize`, so it has to stay behind a function-local import rather than riding along at module
    import time."""
    heavy = ["fastapi", "onnxruntime", "langgraph", "langfuse", "litellm", "fastembed", "ingot.optimize"]
    program = f"import sys, ingot.cli; print([m for m in {heavy!r} if m in sys.modules])"

    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


# --- artifact fidelity ----------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + b"\xff\xfe\xfd"


def test_a_binary_asset_survives_admission_byte_for_byte(tmp_path, monkeypatch):
    """The defect this exists to stop: admission used to reduce a package to decoded text, so an
    image was reviewed as part of the candidate and then was not in it."""
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf")
    (package / "assets").mkdir()
    (package / "assets" / "logo.png").write_bytes(PNG)

    admission.add_package(package, actor="operator")

    entry = _entry(P.load_pending("pdf"), "assets/logo.png")
    assert entry["size"] == len(PNG)
    assert entry["sha256"] == hashlib.sha256(PNG).hexdigest()
    staged = tree.staged_dir(P.load_pending("pdf")["tree"]["digest"])
    assert (staged / "assets" / "logo.png").read_bytes() == PNG


def test_the_approved_revision_changes_when_an_asset_changes(tmp_path, monkeypatch):
    """The whole point of binding a revision. While assets were dropped, two packages differing
    only in an image hashed identically, so approving one approved the other."""
    _store(tmp_path, monkeypatch)
    first = _package(tmp_path / "one", "pdf")
    (first / "logo.png").write_bytes(PNG)
    second = _package(tmp_path / "two", "pdf")
    (second / "logo.png").write_bytes(PNG[:-1] + b"\x00")

    one = admission.add_package(first, actor="operator")["candidate"]["candidate_revision"]
    P.pending_path("pdf").unlink()
    two = admission.add_package(second, actor="operator")["candidate"]["candidate_revision"]

    assert one != two


def test_the_executable_bit_is_recorded_and_nothing_else_is(tmp_path, monkeypatch):
    """Git stores two modes. Recording the raw one would describe a file the vault cannot serve."""
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf", files={"run.sh": "#!/bin/sh\necho hi\n",
                                                       "notes.md": "# Notes\n"})
    (package / "run.sh").chmod(0o777)

    admission.add_package(package, actor="operator")

    pending = P.load_pending("pdf")
    assert _entry(pending, "run.sh")["mode"] == 0o755
    assert _entry(pending, "notes.md")["mode"] == 0o644


def test_a_symlink_is_refused_and_the_source_is_left_alone(tmp_path, monkeypatch):
    """Admission stages exact bytes. Preserving a link puts a path into the vault that leads out of
    the library; flattening it changes the artifact's shape. Neither is admission's call."""
    root = _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf", files={"notes.md": "# Notes\n"})
    (package / "link.md").symlink_to(package / "notes.md")
    before = sorted(item.name for item in package.iterdir())

    with pytest.raises(admission.AdmissionRefused, match="symlink-unsupported"):
        admission.add_package(package, actor="operator")

    assert sorted(item.name for item in package.iterdir()) == before
    assert P.load_pending("pdf") is None
    assert not (root / "pdf").exists()


def test_a_binary_asset_does_not_arrive_as_a_decoded_component(tmp_path, monkeypatch):
    """Two descriptions of one file is one description too many: the receipt would carry a decoded
    copy beside the exact bytes, and the publisher would have to pick."""
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf", files={"notes.md": "# Notes\n"})

    admission.add_package(package, actor="operator")

    components = P.load_pending("pdf")["challenger_components"]
    assert sorted(components) == ["body", "description", "frontmatter"]


def test_an_ingested_package_is_approvable_the_moment_it_is_quarantined(tmp_path, monkeypatch):
    """The freshness check has to recompute the revision the way admission computed it. Deriving it
    from the decoded components instead makes every package with a second file permanently stale:
    quarantined, reviewable, and impossible to approve."""
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf", files={"notes.md": "# Notes\n"})
    admission.add_package(package, actor="operator")

    assert P.stale_evidence_reason("pdf", P.load_pending("pdf")) is None


def test_an_ingested_package_whose_staged_bytes_moved_is_stale(tmp_path, monkeypatch):
    """The other direction. A freshness check that cannot go stale is not checking anything."""
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf", files={"notes.md": "# Notes\n"})
    admission.add_package(package, actor="operator")
    pending = P.load_pending("pdf")
    staged = tree.staged_dir(pending["tree"]["digest"])
    (staged / "notes.md").write_text("# Substituted\n", encoding="utf-8")

    assert P.stale_evidence_reason("pdf", pending) is not None


def test_an_ingested_package_survives_the_whole_cli_lane(tmp_path, monkeypatch, capsys):
    """add, then approve, on the real verbs. The unit tests queue publications directly, so this is
    the only place the approval path itself is exercised on an ingested package."""
    _store(tmp_path, monkeypatch)
    package = _package(tmp_path / "src", "pdf", files={"notes.md": "# Notes\n"})
    assert cli.main(["add", f"file:{package}"]) == 0
    capsys.readouterr()

    assert cli.main(["approve", "pdf", "--actor", "operator"]) == 0

    from ingot.optimize import publication as Q
    receipt = Q.publication_for_skill("pdf")
    assert receipt["state"] == "approved_publishing"
    assert receipt["candidate_revision"] == P.load_pending("pdf")["evidence"]["challenger"]["revision"]


def _entry(pending, path):
    return next(item for item in pending["tree"]["files"] if item["path"] == path)
