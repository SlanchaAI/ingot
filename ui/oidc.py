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
    (the login-flow replay defense). `sub` is required (OIDC mandates it, and it is the audit
    subject), and a multi-audience token must name `audience` in `azp`."""
    # Header parse + key resolution run on untrusted input; a malformed token or JWKS must surface as
    # InvalidToken (401), never an uncaught error (500).
    try:
        header = jwt.get_unverified_header(token)
        key = _key_for_kid(jwks, header.get("kid"))
    except (jwt.PyJWTError, ValueError, TypeError, KeyError) as e:
        raise InvalidToken(f"malformed token or JWKS: {e}") from e
    if key is None:
        raise InvalidToken("no JWKS key matches the token's kid")
    try:
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=audience, issuer=issuer, leeway=leeway,
            options={"require": ["exp", "iat", "aud", "iss", "sub"]})
    except jwt.PyJWTError as e:
        raise InvalidToken(str(e)) from e
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != audience:
        raise InvalidToken("multi-audience token without a matching azp")
    if nonce is not None and claims.get("nonce") != nonce:
        raise InvalidToken("nonce mismatch")
    return claims
