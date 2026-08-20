"""One complete local ingest path: a directory on disk becomes a quarantined proposal.

The whole product claim lives in this file's one guarantee -- **`ingot add` never activates
anything**. It runs the deterministic review, computes the exact revision the library would serve,
records where the package came from, and takes the review slot. The served library is not touched.

This is an adapter, not a second admission service. Path validation, component assembly, content
hashing, evidence writing, pending-record creation, and the atomic slot claim all belong to
`ingot.optimize.ingress` and are called, not reimplemented. What is new here is only the part that is
genuinely new: turning a directory into the fields that service already takes, and binding a
provenance manifest to the result.

`optimize` is imported inside the function rather than at module scope. `ingot list` and
`ingot review` promise to run in a bare virtualenv, and a module-level import here would put the
optimizer on their import path."""
from __future__ import annotations

import time
from pathlib import Path

from . import records
from .parse import ERROR, WARNING, parse_raw
from .review import REVIEW_SCHEMA, review_package

_SUPPORTED_SCHEMES = ("file", "github")


class AdmissionRefused(Exception):
    """The package cannot be represented as a candidate. Nothing was written."""


def parse_locator(locator: str) -> tuple[str, Path | str]:
    """A file path or GitHub repository. Unknown schemes are refused by name."""
    if locator.startswith("file:"):
        return "file", Path(locator[len("file:"):]).expanduser().resolve()
    if locator.startswith("github:"):
        return "github", locator[len("github:"):]

    head, separator, _ = locator.partition(":")
    if separator and head.isalpha() and len(head) > 1:
        raise ValueError(
            f"unsupported source scheme {head!r}; this version supports "
            f"{', '.join(f'{s}:' for s in _SUPPORTED_SCHEMES)} and bare paths")
    return "file", Path(locator).expanduser().resolve()


def _codes(result: dict, level: str) -> list[str]:
    return [finding["code"]
            for section in result["sections"].values()
            for finding in section["findings"]
            if finding["level"] == level]


def add_package(package: Path, *, actor: str, producer: str = "ingot-cli",
                source_type: str = "file", locator: str | None = None,
                provenance: dict | None = None) -> dict:
    """Review, quarantine, and report. Leaves the served library byte-identical."""
    from ingot.mcp_server import registry
    from ingot.mcp_server.registry import read_components, skill_revision
    from ingot.optimize import ingress, tree

    package = Path(package).expanduser().resolve()
    if not package.is_dir():
        raise AdmissionRefused(f"{package} is not a directory")
    source_locator = locator or str(package)

    # `registry.library_dir()` resolved per call, never bound at import: a frozen copy would
    # check collisions
    # against a different library than the one this process actually serves.
    report = review_package(package, library_root=registry.library_dir())
    errors, warnings = _codes(report, ERROR), _codes(report, WARNING)
    if not report["valid"]:
        raise AdmissionRefused(
            f"{package.name} is not admissible: {', '.join(errors)}")

    raw = parse_raw((package / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
    frontmatter = raw.frontmatter or {}
    skill = str(frontmatter.get("name") or package.name)

    # No `file:` components. The package's files travel as a staged tree of exact bytes; carrying
    # decoded copies of the text ones beside it would be a second description of the same files,
    # and the two would eventually disagree about which is authoritative.
    read = read_components(package)
    components, metadata = ingress.build_components(
        skill, read["description"], read["body"], {}, frontmatter)
    try:
        candidate_tree = tree.build(package)
        # Staged before the revision is computed, because the revision *is* the result of
        # materializing the staged tree -- deriving it any other way would be a second description
        # of the same bytes, and the two would eventually disagree. Staging is named by the tree
        # digest and so is idempotent: a submission refused further down leaves nothing behind but
        # a directory the next identical one reuses.
        tree.stage(package, candidate_tree)
    except ValueError as error:
        raise AdmissionRefused(f"{package.name} is not admissible: {error}") from error

    manifest = records.candidate_manifest(
        kind="creation",
        skill=skill,
        source_type=source_type,
        locator=source_locator,
        # What the source resolved to, and what the library will serve. Equal for a package that is
        # already canonical, and deliberately separate fields because they are not always equal --
        # admission collapses whitespace in a description, and then the two diverge.
        resolved_revision=skill_revision(package),
        candidate_revision=tree.revision(skill, candidate_tree, components),
        review={"schema_version": REVIEW_SCHEMA,
                "valid": report["valid"],
                "errors": errors,
                "warnings": warnings,
                "report_digest": records.digest(report)},
        created_at=int(time.time()),
        provenance=({**(provenance or {}), "content_digest": candidate_tree["digest"]}
                    if provenance is not None else None))

    problems = records.validate_candidate(manifest)
    if problems:
        raise AdmissionRefused("the candidate manifest is malformed: " + "; ".join(problems))

    outcome = ingress.submit_package_ingest(
        skill=skill,
        components=components,
        candidate_tree=candidate_tree,
        metadata=metadata,
        revision=manifest["candidate_revision"],
        source=(source_locator if source_locator.startswith(f"{source_type}:")
                else f"{source_type}:{source_locator}"),
        candidate=manifest,
        identity=records.candidate_identity(manifest),
        review_summary=warnings,
        producer=producer,
        caller=actor)
    return {**outcome, "candidate": manifest}
