"""Sign-in-with-Google browser flow (ui/oidc_flow.py) + the mode-aware gate (ui/auth.py), driven
by the forged-token FakeIdp so there is no IdP and no network. The provider's discovery / JWKS /
token endpoints are stubbed; the login->callback dance, state/nonce binding, domain allowlist,
email->role mapping, and endpoint role gating are exercised end to end through a TestClient.

The real-provider version of this (a live Keycloak, the same flow) is tests/test_keycloak_integration.py.
"""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from ui import auth, oidc_flow


def build_oidc_app() -> FastAPI:
    """Mirror ui/app.py's OIDC wiring on a minimal app: session middleware, the /auth/* router, the
    app-wide auth gate, an index that bounces to login, and role-gated state-changing routes."""
    app = FastAPI(dependencies=[Depends(auth.require_auth)])
    app.add_middleware(SessionMiddleware, secret_key="x" * 32, **auth.oidc_cookie_kwargs())
    app.include_router(oidc_flow.router)

    @app.get("/")
    def index(request: Request):
        if not request.session.get("user"):
            return RedirectResponse("/auth/login")
        return {"ok": True}

    @app.post("/api/optimize/x", dependencies=[Depends(auth.require_role("proposer"))])
    def optimize():
        return {"ok": "proposer"}

    @app.post("/api/promote/x", dependencies=[Depends(auth.require_role("approver"))])
    def promote(actor: str = Depends(auth.current_actor)):
        return {"actor": actor}

    return app


@pytest.fixture(autouse=True)
def _oidc_env(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_CLIENT_ID", "ingot")
    monkeypatch.setenv("OIDC_REDIRECT_URL", "http://localhost:8080/auth/callback")  # http -> cookie not Secure
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("OIDC_ALLOWED_DOMAINS", "corp.com")
    monkeypatch.setenv("OIDC_ROLE_MAP", "alice@corp.com:admin,carol@corp.com:proposer")
    monkeypatch.delenv("OIDC_ROLE_CLAIM", raising=False)   # defaults to "email"
    oidc_flow._cache.clear()


@pytest.fixture
def stub_provider(monkeypatch, idp):
    """Point discovery/JWKS at the FakeIdp and make the token endpoint return whatever token the
    test stashes (so the token can echo the per-login nonce, as a real IdP would)."""
    disc = {"authorization_endpoint": "https://idp.test/authorize",
            "token_endpoint": "https://idp.test/token", "jwks_uri": "https://idp.test/jwks",
            "issuer": idp.issuer}
    monkeypatch.setattr(oidc_flow, "_discovery", lambda issuer: disc)
    monkeypatch.setattr(oidc_flow, "_jwks_for_token", lambda uri, kid: idp.jwks)
    holder = {"id_token": None}
    monkeypatch.setattr(oidc_flow.httpx, "post",
                        lambda *a, **k: SimpleNamespace(status_code=200,
                                                        json=lambda: {"id_token": holder["id_token"]}))
    return holder


def _login(client):
    """GET /auth/login, return (state, nonce) parsed from the authorize redirect."""
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code in (302, 307)
    q = parse_qs(urlparse(r.headers["location"]).query)
    return q["state"][0], q["nonce"][0]


def _complete_login(client, idp, holder, **claims):
    state, nonce = _login(client)
    holder["id_token"] = idp.id_token(nonce=nonce, **claims)
    return client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)


# --- the authorize redirect carries PKCE + state + nonce --------------------------------------

def test_login_redirect_has_pkce_state_and_nonce(stub_provider):
    client = TestClient(build_oidc_app())
    r = client.get("/auth/login", follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["response_type"] == ["code"] and q["client_id"] == ["ingot"]
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"][0]
    assert q["state"][0] and q["nonce"][0]
    assert q["redirect_uri"] == ["http://localhost:8080/auth/callback"]


# --- happy path -------------------------------------------------------------------------------

def test_full_login_maps_email_to_role_and_sets_session(stub_provider, idp):
    client = TestClient(build_oidc_app())
    cb = _complete_login(client, idp, stub_provider, sub="a", email="alice@corp.com",
                         email_verified=True, hd="corp.com", name="Alice")
    assert cb.status_code == 303 and cb.headers["location"] == "/"

    me = client.get("/auth/me").json()
    assert me == {"authenticated": True, "email": "alice@corp.com", "name": "Alice", "role": "admin"}
    # admin satisfies the approver-gated route, and the actor is the SSO email
    assert client.post("/api/promote/x").json() == {"actor": "alice@corp.com"}


def test_domain_member_without_a_mapped_role_is_viewer_and_blocked(stub_provider, idp):
    client = TestClient(build_oidc_app())
    _complete_login(client, idp, stub_provider, email="bob@corp.com", email_verified=True, hd="corp.com")
    assert client.get("/auth/me").json()["role"] == "viewer"
    assert client.post("/api/promote/x").status_code == 403   # viewer < approver
    assert client.post("/api/optimize/x").status_code == 403   # viewer < proposer


def test_proposer_can_optimize_but_not_promote(stub_provider, idp):
    client = TestClient(build_oidc_app())
    _complete_login(client, idp, stub_provider, email="carol@corp.com", email_verified=True, hd="corp.com")
    assert client.post("/api/optimize/x").status_code == 200
    assert client.post("/api/promote/x").status_code == 403


# --- domain allowlist -------------------------------------------------------------------------

def test_login_from_wrong_domain_is_refused(stub_provider, idp):
    client = TestClient(build_oidc_app())
    cb = _complete_login(client, idp, stub_provider, email="mallory@evil.com",
                         email_verified=True, hd="evil.com")
    assert cb.status_code == 403


def test_unverified_email_is_refused(stub_provider, idp):
    client = TestClient(build_oidc_app())
    cb = _complete_login(client, idp, stub_provider, email="alice@corp.com",
                         email_verified=False, hd="corp.com")
    assert cb.status_code == 403


def test_personal_gmail_without_hd_is_refused(stub_provider, idp):
    client = TestClient(build_oidc_app())
    cb = _complete_login(client, idp, stub_provider, email="someone@gmail.com", email_verified=True)
    assert cb.status_code == 403


# --- callback binding / errors ----------------------------------------------------------------

def test_state_mismatch_is_rejected(stub_provider, idp):
    client = TestClient(build_oidc_app())
    _login(client)
    stub_provider["id_token"] = idp.id_token(email="alice@corp.com", email_verified=True, hd="corp.com")
    assert client.get("/auth/callback?code=abc&state=forged", follow_redirects=False).status_code == 401


def test_callback_without_a_login_session_is_rejected(stub_provider, idp):
    client = TestClient(build_oidc_app())
    assert client.get("/auth/callback?code=abc&state=whatever", follow_redirects=False).status_code == 401


def test_nonce_mismatch_is_rejected(stub_provider, idp):
    client = TestClient(build_oidc_app())
    state, _nonce = _login(client)
    stub_provider["id_token"] = idp.id_token(email="alice@corp.com", email_verified=True,
                                             hd="corp.com", nonce="not-the-login-nonce")
    assert client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False).status_code == 401


def test_provider_error_param_is_surfaced_as_401(stub_provider):
    client = TestClient(build_oidc_app())
    _login(client)
    r = client.get("/auth/callback?error=access_denied&error_description=nope", follow_redirects=False)
    assert r.status_code == 401


# --- gate + index behavior --------------------------------------------------------------------

def test_unauthenticated_index_redirects_to_login(stub_provider):
    client = TestClient(build_oidc_app())
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/auth/login"


def test_unauthenticated_api_is_401(stub_provider):
    client = TestClient(build_oidc_app())
    assert client.post("/api/promote/x").status_code == 401


def test_logout_clears_the_session(stub_provider, idp):
    client = TestClient(build_oidc_app())
    _complete_login(client, idp, stub_provider, email="alice@corp.com", email_verified=True, hd="corp.com")
    assert client.get("/auth/me").json()["authenticated"] is True
    client.get("/auth/logout", follow_redirects=False)
    assert client.get("/auth/me").status_code == 401   # session gone -> gate refuses


# --- fail-closed config + RBAC-off-outside-OIDC -----------------------------------------------

def test_validate_oidc_config_fails_closed_on_missing_keys(monkeypatch):
    monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="OIDC_CLIENT_ID"):
        auth.validate_oidc_config()


def test_validate_oidc_config_rejects_a_weak_session_secret(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "short")
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        auth.validate_oidc_config()


def test_require_role_is_a_noop_outside_oidc_mode(monkeypatch):
    # password/open mode is a single trust domain: RBAC does not apply, so no session is needed
    monkeypatch.setenv("AUTH_MODE", "open")
    auth.require_role("admin")(SimpleNamespace())   # must not raise despite no .session


# --- token-exchange failure at the callback ---------------------------------------------------

def test_token_endpoint_error_is_401(stub_provider, idp, monkeypatch):
    client = TestClient(build_oidc_app())
    state, _ = _login(client)
    monkeypatch.setattr(oidc_flow.httpx, "post",
                        lambda *a, **k: SimpleNamespace(status_code=400,
                                                        json=lambda: {"error": "invalid_grant"}))
    assert client.get(f"/auth/callback?code=abc&state={state}",
                      follow_redirects=False).status_code == 401


def test_token_response_without_id_token_is_401(stub_provider, idp):
    client = TestClient(build_oidc_app())
    state, _ = _login(client)
    stub_provider["id_token"] = None   # token endpoint answers 200 but omits the id_token
    assert client.get(f"/auth/callback?code=abc&state={state}",
                      follow_redirects=False).status_code == 401


# --- discovery / JWKS caching + key rotation (the real network helpers, httpx.get stubbed) -----

class _Resp:
    def __init__(self, payload):
        self._payload, self.status_code = payload, 200

    def json(self):
        return self._payload


def test_discovery_is_cached_per_issuer(monkeypatch):
    calls = []
    monkeypatch.setattr(oidc_flow.httpx, "get",
                        lambda url, timeout=None: calls.append(url) or _Resp({"issuer": "x"}))
    first = oidc_flow._discovery("https://idp.test")
    second = oidc_flow._discovery("https://idp.test")
    assert first == second and len(calls) == 1   # second call served from cache


def test_jwks_is_cached_within_ttl_and_refetched_after(monkeypatch):
    calls = []
    monkeypatch.setattr(oidc_flow.httpx, "get",
                        lambda url, timeout=None: calls.append(url) or _Resp({"keys": [{"kid": "k"}]}))
    now = [1000.0]
    monkeypatch.setattr(oidc_flow.time, "time", lambda: now[0])
    oidc_flow._jwks("https://idp.test/jwks")
    oidc_flow._jwks("https://idp.test/jwks")          # same instant: cache hit
    assert len(calls) == 1
    now[0] += oidc_flow._JWKS_TTL + 1                  # age past the TTL
    oidc_flow._jwks("https://idp.test/jwks")
    assert len(calls) == 2                             # refetched


def test_jwks_refetches_once_on_an_unknown_kid(monkeypatch):
    # key rotation: the token's kid is not in the cached JWKS, so force a single refetch
    responses = [{"keys": [{"kid": "old"}]}, {"keys": [{"kid": "old"}, {"kid": "new"}]}]
    calls = []

    def fake_get(url, timeout=None):
        resp = _Resp(responses[min(len(calls), len(responses) - 1)])
        calls.append(url)
        return resp

    monkeypatch.setattr(oidc_flow.httpx, "get", fake_get)
    jwks = oidc_flow._jwks_for_token("https://idp.test/jwks", "new")
    assert len(calls) == 2                             # initial (stale) fetch + rotation refetch
    assert {k["kid"] for k in jwks["keys"]} == {"old", "new"}


def test_jwks_does_not_refetch_for_a_known_kid(monkeypatch):
    calls = []
    monkeypatch.setattr(oidc_flow.httpx, "get",
                        lambda url, timeout=None: calls.append(url) or _Resp({"keys": [{"kid": "k1"}]}))
    oidc_flow._jwks_for_token("https://idp.test/jwks", "k1")
    assert len(calls) == 1                             # kid present, no rotation refetch


# --- cookie flags, actor fallback, domain gate ------------------------------------------------

def test_cookie_is_secure_only_over_https(monkeypatch):
    monkeypatch.setenv("OIDC_REDIRECT_URL", "https://ingot.corp/auth/callback")
    monkeypatch.setenv("SESSION_MAX_AGE", "1234")
    kw = auth.oidc_cookie_kwargs()
    assert kw == {"same_site": "lax", "https_only": True, "max_age": 1234}
    monkeypatch.setenv("OIDC_REDIRECT_URL", "http://localhost:8080/auth/callback")
    assert auth.oidc_cookie_kwargs()["https_only"] is False   # plain http: no Secure flag (local dev)


def test_current_actor_falls_back_to_sub_then_placeholder():
    # AUTH_MODE=oidc is set by the autouse fixture; the audit actor prefers email, then sub
    assert auth.current_actor(SimpleNamespace(session={"user": {"sub": "s-1"}})) == "s-1"
    assert auth.current_actor(SimpleNamespace(session={"user": {"name": "n"}})) == "sso-user"
    with pytest.raises(HTTPException):
        auth.current_actor(SimpleNamespace(session={}))


def test_check_domain_accepts_any_listed_domain_case_insensitively():
    allowed = ["corp.com", "sub.corp.com"]
    oidc_flow._check_domain({"email_verified": True, "hd": "CORP.COM"}, allowed)      # case-folded
    oidc_flow._check_domain({"email_verified": True, "hd": "sub.corp.com"}, allowed)  # second domain
    with pytest.raises(HTTPException):
        oidc_flow._check_domain({"email_verified": True, "hd": "other.com"}, allowed)
    # empty allowlist disables the domain gate but never the verified-email requirement
    oidc_flow._check_domain({"email_verified": True, "hd": "anything.com"}, [])
    with pytest.raises(HTTPException):
        oidc_flow._check_domain({"hd": "anything.com"}, [])
