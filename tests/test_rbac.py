"""Unit tests for the provider-agnostic RBAC + OIDC-claims core (no IdP, no network)."""
import pytest
from fastapi import HTTPException

from ui import rbac


def test_role_ranking_is_least_to_most_privileged():
    assert rbac.has_role("admin", "viewer")
    assert rbac.has_role("approver", "approver")
    assert not rbac.has_role("proposer", "approver")
    assert not rbac.has_role("viewer", "proposer")
    assert not rbac.has_role("nonsense", "viewer")   # unknown role satisfies nothing


def test_parse_role_map_keeps_valid_and_drops_junk():
    m = rbac.parse_role_map(" ingot-approver:approver , ingot-admin:admin , bad:notarole , nocolon ")
    assert m == {"ingot-approver": "approver", "ingot-admin": "admin"}   # unknown app role + junk dropped
    assert rbac.parse_role_map("") == {} and rbac.parse_role_map(None) == {}


def test_role_from_claims_takes_the_highest_grant_else_default():
    role_map = {"g-view": "viewer", "g-approve": "approver", "g-admin": "admin"}
    assert rbac.role_from_claims(["g-view", "g-admin", "g-approve"], role_map) == "admin"  # highest wins
    assert rbac.role_from_claims(["g-view"], role_map) == "viewer"
    assert rbac.role_from_claims(["unmapped"], role_map) == "viewer"      # default
    assert rbac.role_from_claims([], role_map) == "viewer"


def test_authorize_enforces_the_required_role():
    rbac.authorize("approver", "approver")               # no raise
    rbac.authorize("admin", "approver")                  # higher is fine
    with pytest.raises(HTTPException) as e:
        rbac.authorize("viewer", "approver")
    assert e.value.status_code == 403 and "approver" in e.value.detail


def test_identity_from_entra_app_roles_claim():
    # Entra puts app roles in `roles`; may arrive as a single string
    role_map = {"ingot-admin": "admin"}
    ident = rbac.identity_from_claims(
        {"sub": "abc", "name": "Alice", "preferred_username": "alice@corp.com", "roles": "ingot-admin"},
        role_claim="roles", role_map=role_map)
    assert ident == {"sub": "abc", "email": "alice@corp.com", "name": "Alice", "role": "admin"}


def test_identity_from_okta_groups_claim_and_email_fallback():
    # Okta carries groups in `groups`; here `email` is present and used directly
    role_map = {"eng-approvers": "approver"}
    ident = rbac.identity_from_claims(
        {"sub": "xyz", "email": "bob@corp.com", "groups": ["all-staff", "eng-approvers"]},
        role_claim="groups", role_map=role_map)
    assert ident["email"] == "bob@corp.com" and ident["role"] == "approver"


def test_identity_defaults_to_viewer_with_no_matching_claims():
    ident = rbac.identity_from_claims({"sub": "s", "email": "e@corp.com", "groups": ["random"]},
                                      role_claim="groups", role_map={"eng-approvers": "approver"})
    assert ident["role"] == "viewer"


def test_admin_satisfies_every_role_requirement():
    # role inheritance: admin must pass every gate without special-casing (rank comparison,
    # never exact match)
    for required in rbac.ROLES:
        rbac.authorize("admin", required)             # no raise


def test_role_map_matching_is_exact_and_case_sensitive():
    role_map = {"g-admin": "admin"}
    assert rbac.role_from_claims(["G-Admin"], role_map) == "viewer"    # case differs: no grant
    assert rbac.role_from_claims(["g-admin-2"], role_map) == "viewer"  # no prefix/substring match
    assert rbac.role_from_claims([" g-admin"], role_map) == "viewer"   # no trimming of claim values


def test_role_from_claims_handles_none():
    assert rbac.role_from_claims(None, {"g-admin": "admin"}) == "viewer"


def test_identity_from_empty_claims_is_anonymous_viewer():
    # a token with none of the optional claims yields empty identity fields and the floor role,
    # not a KeyError
    assert rbac.identity_from_claims({}) == {"sub": "", "email": "", "name": "", "role": "viewer"}


def test_malformed_role_claims_fail_closed_not_crash():
    # a malformed claim shape must yield no roles (viewer), never a TypeError -> 500
    role_map = {"g-admin": "admin"}

    def role_of(claim_value):
        return rbac.identity_from_claims({"sub": "s", "roles": claim_value},
                                         role_claim="roles", role_map=role_map)["role"]

    assert role_of(42) == "viewer"                       # number, not iterable
    assert role_of({"g-admin": True}) == "viewer"        # object, not string/list
    assert role_of([{"nested": "object"}]) == "viewer"   # unhashable list entry
    assert role_of([None, 7]) == "viewer"                # non-string entries
    assert role_of(["g-admin", {"x": 1}]) == "admin"     # valid string entries still count
