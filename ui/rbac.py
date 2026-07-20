"""Role-based authorization for the change-control UI (see docs/superpowers/specs/2026-07-19-sso-rbac-design.md).

Pure, provider-agnostic authorization logic: map an authenticated identity's OIDC claims to one app
role, and gate actions by role. The *authentication* source (LAN password today, OIDC later) supplies
the identity and its raw role/group claim values; this module never talks to an IdP, so it is fully
unit-testable now, independent of the deferred provider integration and the self-hosted-vs-SaaS call.

Wiring `require_role` into the endpoints and populating the identity from an OIDC session is the
implementation phase; this is the decision-independent core it will build on.
"""
from __future__ import annotations

from fastapi import HTTPException

# Least- to most-privileged. viewer: read the board/evidence/history; proposer: trigger candidate
# generation; approver: promote/reject/rollback; admin: the above + configuration.
ROLES = ("viewer", "proposer", "approver", "admin")
_RANK = {role: i for i, role in enumerate(ROLES)}
DEFAULT_ROLE = "viewer"   # a valid session with no mapped role can look, not change


def parse_role_map(spec: str | None) -> dict[str, str]:
    """`'ingot-approver:approver,ingot-admin:admin'` -> {provider_value: app_role}. Entries whose app
    role is not a known ROLE are dropped, so a typo can't silently grant access."""
    mapping: dict[str, str] = {}
    for pair in (spec or "").split(","):
        provider, sep, role = pair.partition(":")
        provider, role = provider.strip(), role.strip()
        if sep and provider and role in _RANK:
            mapping[provider] = role
    return mapping


def role_from_claims(claim_values, role_map: dict[str, str], default: str = DEFAULT_ROLE) -> str:
    """The highest app role granted by any of the identity's role/group claim values, else `default`."""
    granted = [role_map[v] for v in (claim_values or []) if v in role_map]
    return max(granted, key=lambda r: _RANK[r]) if granted else default


def has_role(user_role: str, required: str) -> bool:
    """True when `user_role` is at least as privileged as `required`."""
    return _RANK.get(user_role, -1) >= _RANK[required]


def authorize(user_role: str, required: str) -> None:
    """Raise 403 unless `user_role` satisfies `required`, the primitive `require_role` will call."""
    if not has_role(user_role, required):
        raise HTTPException(status_code=403,
                            detail=f"requires the '{required}' role; you have '{user_role}'")


def identity_from_claims(claims: dict, role_claim: str = "roles",
                         role_map: dict[str, str] | None = None) -> dict:
    """OIDC ID-token claims -> {sub, email, name, role}. `role_claim` names the claim carrying the
    app-role/group values, Entra app roles use `roles`, Okta groups use `groups`. The value may be a
    single string or a list; email falls back to `preferred_username`. Parsing fails closed: any
    other claim shape (number, object, non-string list entries) contributes no roles rather than
    erroring, so a malformed token can only lose privileges."""
    values = claims.get(role_claim) or []
    if isinstance(values, str):
        values = [values]
    elif isinstance(values, list):
        values = [v for v in values if isinstance(v, str)]
    else:
        values = []
    return {
        "sub": claims.get("sub", ""),
        "email": claims.get("email") or claims.get("preferred_username", ""),
        "name": claims.get("name", ""),
        "role": role_from_claims(values, role_map or {}),
    }
