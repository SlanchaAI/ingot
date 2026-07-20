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
from urllib.parse import parse_qs, urlparse

import pytest

KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL")
REALM = os.environ.get("KEYCLOAK_REALM", "ingot")

pytestmark = pytest.mark.skipif(not KEYCLOAK_URL,
                                reason="set KEYCLOAK_URL (compose --profile sso up keycloak) to run")


def _issuer() -> str:
    return f"{KEYCLOAK_URL.rstrip('/')}/realms/{REALM}"


def _discovery():
    """Fetch the discovery doc, retrying while Keycloak boots (start-dev + realm import can take
    ~30s after `compose up` returns; without this the test races the container)."""
    import time

    import httpx
    deadline = time.monotonic() + 120
    while True:
        try:
            resp = httpx.get(f"{_issuer()}/.well-known/openid-configuration", timeout=15)
            if resp.status_code == 200:
                return resp.json()
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(f"Keycloak at {KEYCLOAK_URL} not ready after 120s")
        time.sleep(2)


def _token_and_jwks():
    import httpx
    disc = _discovery()
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


def test_real_keycloak_full_browser_flow_end_to_end(monkeypatch):
    """Drive the actual /auth/login -> provider login form -> /auth/callback browser flow against a
    live Keycloak, the same code path Google uses. Keycloak stands in for Google in CI (a real Google
    login can't be scripted); the roles come from Keycloak's `roles` claim rather than an email map,
    and no domain allowlist is set (Keycloak tokens carry no `hd`)."""
    import html
    import re

    import httpx
    from fastapi.testclient import TestClient

    # Configure the app exactly as ui/app.py would for OIDC, pointed at Keycloak.
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_ISSUER", _issuer())
    monkeypatch.setenv("OIDC_CLIENT_ID", "ingot")
    monkeypatch.setenv("OIDC_REDIRECT_URL", "http://localhost:8080/auth/callback")
    monkeypatch.setenv("OIDC_ROLE_CLAIM", "roles")
    monkeypatch.setenv("OIDC_ROLE_MAP", "ingot-approver:approver,ingot-admin:admin")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.delenv("OIDC_ALLOWED_DOMAINS", raising=False)

    from tests.test_oidc_flow import build_oidc_app
    from ui import oidc_flow
    oidc_flow._cache.clear()
    client = TestClient(build_oidc_app())

    # 1. /auth/login -> the provider's authorize URL (and our signed session cookie is set).
    login = client.get("/auth/login", follow_redirects=False)
    authorize_url = login.headers["location"]

    # 2. Act as the browser against real Keycloak: fetch the login page, submit the test user's creds.
    #    Keycloak sets its session cookies `Secure`; Python's cookiejar won't resend those over http
    #    (a rule browsers exempt for localhost), so carry them across the POST explicitly.
    with httpx.Client(follow_redirects=False, timeout=15) as browser:
        page = browser.get(authorize_url)
        cookie_header = "; ".join(f"{k}={v}" for k, v in browser.cookies.items())
        action = html.unescape(re.search(r'id="kc-form-login"[^>]*action="([^"]+)"', page.text).group(1))
        posted = browser.post(action, data={"username": "approver", "password": "approver-pw"},
                              headers={"Cookie": cookie_header})
    # 3. Keycloak redirects to our callback with the code; pull code+state off the Location.
    assert posted.status_code in (302, 303), f"Keycloak did not redirect: {posted.status_code}"
    cb_query = parse_qs(urlparse(posted.headers["location"]).query)
    assert "code" in cb_query, f"no auth code in redirect: {posted.headers.get('location')}"

    # 4. Feed the callback back into our app (it redeems the code with real Keycloak and sets session).
    cb = client.get(f"/auth/callback?code={cb_query['code'][0]}&state={cb_query['state'][0]}",
                    follow_redirects=False)
    assert cb.status_code == 303, cb.text

    me = client.get("/auth/me").json()
    assert me["authenticated"] is True
    assert me["email"] == "approver@ingot.test"
    assert me["role"] == "approver"
