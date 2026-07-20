"""Live-OIDC integration test, layer 3 of the SSO/RBAC strategy (a real provider in a container,
between the forged-token unit tests and the hosted-vendor smoke).

Skipped unless KEYCLOAK_URL is set, so the default suite stays fast and hermetic. To run it:

    docker compose --profile sso up -d keycloak
    docker run --rm --network host -e KEYCLOAK_URL=http://localhost:8081 -v "$PWD:/app" -w /app \
      ingot-mcp python -m pytest tests/test_keycloak_integration.py -q

It exercises the real discovery + JWKS + token endpoints against `ui.oidc.verify_id_token` and the
RBAC mapping, no browser flow needed (the test user's token comes via the direct-grant / ROPC path,
which is fine for tests). When the OIDC callback lands, its Playwright test reuses this same realm.
"""
import os

import pytest

KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL")
REALM = os.environ.get("KEYCLOAK_REALM", "ingot")

pytestmark = pytest.mark.skipif(not KEYCLOAK_URL,
                                reason="set KEYCLOAK_URL (compose --profile sso up keycloak) to run")


def _issuer() -> str:
    return f"{KEYCLOAK_URL.rstrip('/')}/realms/{REALM}"


def _token_and_jwks():
    import httpx
    disc = httpx.get(f"{_issuer()}/.well-known/openid-configuration", timeout=15).json()
    jwks = httpx.get(disc["jwks_uri"], timeout=15).json()
    resp = httpx.post(disc["token_endpoint"], timeout=15, data={
        "grant_type": "password", "client_id": "ingot", "scope": "openid",
        "username": "approver", "password": "approver-pw"})
    body = resp.json()
    assert "id_token" in body, f"no id_token from Keycloak: {body}"
    return body["id_token"], jwks


def test_real_keycloak_token_verifies_and_maps_to_role():
    from ui.oidc import verify_id_token
    from ui.rbac import identity_from_claims
    id_token, jwks = _token_and_jwks()
    claims = verify_id_token(id_token, jwks, issuer=_issuer(), audience="ingot")
    identity = identity_from_claims(claims, role_claim="roles",
                                    role_map={"ingot-approver": "approver", "ingot-admin": "admin"})
    assert identity["email"] == "approver@ingot.test"
    assert identity["role"] == "approver"


def test_real_keycloak_tampered_token_is_rejected():
    from ui.oidc import InvalidToken, verify_id_token
    id_token, jwks = _token_and_jwks()
    tampered = id_token[:-4] + ("aaaa" if id_token[-4:] != "aaaa" else "bbbb")   # break the signature
    with pytest.raises(InvalidToken):
        verify_id_token(tampered, jwks, issuer=_issuer(), audience="ingot")
