"""The two versioned records the control plane is built on.

A **candidate manifest** names exactly what is being proposed, where it came from, and what the
deterministic review said about it. A **release receipt** names exactly what was published, against
which champion, on whose authority.

Three properties matter more than the field list:

- **A revision names exact bytes.** Every revision here is a content digest or the absence marker.
  A semantic version is not accepted anywhere: a tag can be moved and a digest cannot.
- **Candidate identity is deterministic.** Two submissions of the same bytes, from different
  checkouts at different times, are the same candidate. That is what makes an identical resubmission
  idempotent instead of a duplicate proposal.
- **A receipt is not a proof.** There is no signature field and there will not be one until there is
  a threat model. A local record that a machine administrator can rewrite must not carry anything
  shaped like evidence that they did not.

Consumers: `ingot add` writes candidate manifests; the publisher writes release receipts once
publication succeeds -- never when approval merely queues it.
"""
from __future__ import annotations

import hashlib
import json
import re

from ingot.mcp_server.registry import SLUG_RE

CANDIDATE_SCHEMA = "ingot/candidate/v1"
RELEASE_SCHEMA = "ingot/release/v1"

# Absence is a revision. A creation displaces nothing; a rollback can restore nothing. The existing
# publisher already treats it this way (optimize/promote.py ABSENT_REVISION), and the two spellings
# must not drift apart.
ABSENT_REVISION = "absent"

SOURCE_TYPES = frozenset({"file", "github", "optimizer", "mcp"})
CANDIDATE_KINDS = frozenset({"creation", "update"})
ACTIONS = frozenset({"promote", "rollback"})
RESULTS = frozenset({"published", "failed"})

_DIGEST = re.compile(r"^[0-9a-f]{64}$")

# Kept for file candidates created before GitHub acquisition. Their proposal IDs already include
# the complete bound review record except circumstance metadata, and changing that basis would
# turn an identical resubmission into a slot conflict during upgrade.
_NOT_IDENTITY = ("created_at", "actor")

def digest(payload: object) -> str:
    """SHA-256 over a canonical JSON encoding. Key order must not change the answer."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_revision(value: object, *, allow_absent: bool = True) -> bool:
    if allow_absent and value == ABSENT_REVISION:
        return True
    return isinstance(value, str) and bool(_DIGEST.fullmatch(value))


def candidate_manifest(*, kind: str, skill: str, source_type: str, locator: str,
                       resolved_revision: str, candidate_revision: str, review: dict,
                       created_at: int, provenance: dict | None = None) -> dict:
    """Build a candidate manifest. Validation is separate -- callers validate what they build."""
    return {
        "schema_version": CANDIDATE_SCHEMA,
        "kind": kind,
        "skill": skill,
        "source": {**(provenance or {}),
                   "type": source_type,
                   "locator": locator,
                   "resolved_revision": resolved_revision},
        "candidate_revision": candidate_revision,
        # `digest` binds this summary; `report_digest`, when the caller supplies one, binds the full
        # report the summary was drawn from. Both travel, so neither the summary nor the report it
        # came from can be edited afterwards without the manifest noticing.
        "review": {"schema_version": review.get("schema_version"),
                   "digest": digest(review),
                   "valid": review.get("valid"),
                   "errors": list(review.get("errors") or []),
                   "warnings": list(review.get("warnings") or []),
                   **({"report_digest": review["report_digest"]}
                      if review.get("report_digest") else {})},
        "created_at": created_at,
    }


def candidate_identity(manifest: dict) -> str:
    """The digest that decides whether two submissions are the same candidate.

    Drops timestamps, actor metadata, and the local path the package happened to be read from.
    Keeps the skill, the kind, the source type, both revisions, and the review outcome -- a review
    is evidence, and evidence is revision-bound, so the same bytes reviewed clean and reviewed with
    errors are not interchangeable proposals."""
    source = manifest.get("source") or {}
    review = manifest.get("review") or {}
    identity_review = ({key: review.get(key) for key in
                        ("schema_version", "valid", "errors", "warnings")}
                       if source.get("type") == "github" else
                       {key: value for key, value in review.items() if key not in _NOT_IDENTITY})
    return digest({
        "schema_version": manifest.get("schema_version"),
        "kind": manifest.get("kind"),
        "skill": manifest.get("skill"),
        "source_type": source.get("type"),
        "resolved_revision": source.get("resolved_revision"),
        "candidate_revision": manifest.get("candidate_revision"),
        # A GitHub report includes its temporary clone path. That path is provenance rather than
        # outcome, so GitHub identity keeps the deterministic result while its manifest keeps both
        # digests. File candidates retain the original formula above for upgrade compatibility.
        "review": identity_review,
    })


def validate_candidate(manifest: dict) -> list[str]:
    """Every problem with a manifest, or an empty list. Never raises: a caller reporting to a person
    wants all of them at once, not the first."""
    problems = []
    if manifest.get("schema_version") != CANDIDATE_SCHEMA:
        problems.append(f"schema_version must be {CANDIDATE_SCHEMA}, "
                        f"found {manifest.get('schema_version')!r}")
    if manifest.get("kind") not in CANDIDATE_KINDS:
        problems.append(f"kind must be one of {sorted(CANDIDATE_KINDS)}, "
                        f"found {manifest.get('kind')!r}")

    skill = manifest.get("skill")
    if not isinstance(skill, str) or not SLUG_RE.fullmatch(skill):
        problems.append(f"skill must be a valid slug, found {skill!r}")

    source = manifest.get("source")
    if not isinstance(source, dict):
        problems.append("source is missing")
    else:
        if source.get("type") not in SOURCE_TYPES:
            problems.append(f"source.type must be one of {sorted(SOURCE_TYPES)}, "
                            f"found {source.get('type')!r}")
        if not isinstance(source.get("locator"), str) or not source.get("locator"):
            problems.append("source.locator is missing")
        if not _is_revision(source.get("resolved_revision"), allow_absent=False):
            problems.append("source.resolved_revision must be a content digest, not a version: "
                            f"found {source.get('resolved_revision')!r}")

    if not _is_revision(manifest.get("candidate_revision"), allow_absent=False):
        problems.append("candidate_revision must be a content digest, found "
                        f"{manifest.get('candidate_revision')!r}")

    review = manifest.get("review")
    if not isinstance(review, dict):
        problems.append("review is missing")
    elif not _is_revision(review.get("digest"), allow_absent=False):
        problems.append("review.digest must be a content digest")

    if not isinstance(manifest.get("created_at"), int):
        problems.append("created_at must be an integer timestamp")
    return problems


def release_receipt(*, skill: str, action: str, proposal_id: str, publication_id: str,
                    expected_champion: str, candidate_revision: str, evidence_digests: list[str],
                    actor: str, publisher: str, target: str, published_at: int,
                    result: str, error: str | None = None) -> dict:
    """Build a release receipt.

    Emitted only once publication has actually succeeded or failed. Approval that merely queues a
    publication is a different state and does not produce one of these -- a receipt that appeared at
    approval time would claim a skill was released while the served bytes were unchanged."""
    receipt = {
        "schema_version": RELEASE_SCHEMA,
        "skill": skill,
        "action": action,
        "proposal_id": proposal_id,
        "publication_id": publication_id,
        "expected_champion": expected_champion,
        "candidate_revision": candidate_revision,
        "evidence_digests": list(evidence_digests),
        "actor": actor,
        "publisher": publisher,
        "target": target,
        "published_at": published_at,
        "result": result,
    }
    if error is not None:
        receipt["error"] = error
    return receipt


def validate_release(receipt: dict) -> list[str]:
    problems = []
    if receipt.get("schema_version") != RELEASE_SCHEMA:
        problems.append(f"schema_version must be {RELEASE_SCHEMA}, "
                        f"found {receipt.get('schema_version')!r}")

    skill = receipt.get("skill")
    if not isinstance(skill, str) or not SLUG_RE.fullmatch(skill):
        problems.append(f"skill must be a valid slug, found {skill!r}")
    if receipt.get("action") not in ACTIONS:
        problems.append(f"action must be one of {sorted(ACTIONS)}, found {receipt.get('action')!r}")
    if receipt.get("result") not in RESULTS:
        problems.append(f"result must be one of {sorted(RESULTS)}, found {receipt.get('result')!r}")

    for field in ("expected_champion", "candidate_revision"):
        if not _is_revision(receipt.get(field)):
            problems.append(f"{field} must be a content digest or {ABSENT_REVISION!r}, "
                            f"found {receipt.get(field)!r}")

    for field in ("proposal_id", "publication_id", "actor", "publisher", "target"):
        if not isinstance(receipt.get(field), str) or not receipt.get(field):
            problems.append(f"{field} is missing")

    if not isinstance(receipt.get("evidence_digests"), list):
        problems.append("evidence_digests must be a list")
    elif not all(_is_revision(item, allow_absent=False) for item in receipt["evidence_digests"]):
        problems.append("every entry in evidence_digests must be a content digest")

    if not isinstance(receipt.get("published_at"), int):
        problems.append("published_at must be an integer timestamp")

    # A failure that erased its reason leaves an operator with a stalled lane and nothing to read;
    # a success carrying an error is a receipt that disagrees with itself.
    if receipt.get("result") == "failed" and not receipt.get("error"):
        problems.append("a failed receipt must carry an error explaining why")
    if receipt.get("result") == "published" and receipt.get("error"):
        problems.append("a published receipt must not carry an error")
    return problems
