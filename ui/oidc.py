"""OIDC ID-token validation primitive (see docs/superpowers/specs/2026-07-19-sso-rbac-design.md).

Provider-agnostic and network-free: given a JWKS (discovery + fetch + caching happen in the
implementation phase), validate an ID token's signature, `iss`, `aud`, `exp`/`iat`, and `nonce`, and
return its claims. This is the pure verifier the OIDC callback will call once the browser flow is
built; it is decision-independent and unit-tested now against forged tokens (tests/conftest.FakeIdp).
The callback maps `InvalidToken` to an HTTP 401.
"""
from __future__ import annotations

import json

import jwt


class InvalidToken(Exception):
    """The ID token failed validation (bad signature / issuer / audience / expiry / nonce / kid)."""


def _key_for_kid(jwks: dict, kid: str | None):
    for jwk in jwks.get("keys", []):
        if jwk.get("kid") == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
    return None


def verify_id_token(token: str, jwks: dict, issuer: str, audience: str, *,
                    nonce: str | None = None, leeway: int = 60) -> dict:
    """Return the token's claims if valid, else raise `InvalidToken`. `leeway` (seconds) tolerates
    small clock skew on `exp`/`iat`. When `nonce` is given it must match the token's `nonce` claim
    (the login-flow replay defense)."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise InvalidToken(f"malformed token: {e}") from e
    key = _key_for_kid(jwks, header.get("kid"))
    if key is None:
        raise InvalidToken("no JWKS key matches the token's kid")
    try:
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=audience, issuer=issuer, leeway=leeway,
            options={"require": ["exp", "iat", "aud", "iss"]})
    except jwt.PyJWTError as e:
        raise InvalidToken(str(e)) from e
    if nonce is not None and claims.get("nonce") != nonce:
        raise InvalidToken("nonce mismatch")
    return claims
