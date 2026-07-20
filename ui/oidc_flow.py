"""Sign-in-with-Google browser flow (OIDC Authorization Code + PKCE) for the change-control UI.

Google is an ordinary OIDC provider, so this is a generic auth-code+PKCE flow pointed at Google's
issuer (see docs/sso.md and docs/superpowers/specs/2026-07-19-sso-rbac-design.md). `ui/oidc.py` stays
network-free and owns ID-token validation; this module owns the parts that talk to the provider:
discovery, JWKS fetch/caching, the login redirect, and the callback that redeems the code and
establishes the session. Validation still goes through `ui.oidc.verify_id_token` and the session
identity through `ui.rbac.identity_from_claims`, so the audited pieces are the ones already
unit-tested.

Two things are Google-shaped. (1) Access is gated on the **Workspace domain**: Google ID tokens
carry `hd` (hosted domain) but no roles/groups, so `OIDC_ALLOWED_DOMAINS` decides who may sign in
(a personal gmail account has no `hd` and is refused). (2) Roles come from an **email->role map**
(`OIDC_ROLE_CLAIM=email` + `OIDC_ROLE_MAP=alice@corp.com:admin,...`), since Google can't supply
them; a domain member not in the map defaults to `viewer`.

Flow: `/auth/login` mints a fresh `state` + PKCE `code_verifier` + `nonce`, stashes them in the
signed session, and redirects to Google. `/auth/callback` matches `state`, redeems the code once with
that verifier, validates the returned ID token (issuer/audience/exp/iat/nonce), checks the domain,
maps the email to an app role, and stores `{sub, email, name, role}` in the session. Without the
state/verifier/nonce binding, ID-token checks alone leave room for login-CSRF and code injection.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ui.oidc import InvalidToken, verify_id_token
from ui.rbac import identity_from_claims, parse_role_map

router = APIRouter()

_HTTP_TIMEOUT = 15
_JWKS_TTL = 3600           # refetch keys hourly; a token whose kid is unknown forces a refetch too
_lock = threading.Lock()
_cache: dict = {}          # {issuer: discovery_doc}, {jwks_uri: (keys, fetched_at)}


def _config() -> dict:
    """OIDC settings from the environment, read fresh so tests and reconfig take effect. The
    issuer/client/redirect are guaranteed present by `ui.auth.validate_oidc_config` at startup.
    Defaults target Google (issuer, and roles keyed on the email claim)."""
    issuer = (os.environ.get("OIDC_ISSUER") or "https://accounts.google.com").rstrip("/")
    domains = [d.strip().lower() for d in (os.environ.get("OIDC_ALLOWED_DOMAINS") or "").split(",")
               if d.strip()]
    return {
        "issuer": issuer,
        "client_id": os.environ.get("OIDC_CLIENT_ID", ""),
        "client_secret": os.environ.get("OIDC_CLIENT_SECRET") or None,
        "redirect_url": os.environ.get("OIDC_REDIRECT_URL", ""),
        "scope": os.environ.get("OIDC_SCOPE") or "openid email profile",
        "role_claim": os.environ.get("OIDC_ROLE_CLAIM") or "email",
        "role_map": parse_role_map(os.environ.get("OIDC_ROLE_MAP")),
        "allowed_domains": domains,
    }


def _check_domain(claims: dict, allowed_domains: list[str]) -> None:
    """Google-Workspace access gate: refuse a sign-in whose verified hosted domain (`hd`) is not on
    the allowlist. No allowlist means the domain check is off (e.g. the Keycloak CI provider)."""
    if not allowed_domains:
        return
    if not claims.get("email_verified"):
        raise HTTPException(403, "your Google account's email address is not verified")
    if (claims.get("hd") or "").lower() not in allowed_domains:
        raise HTTPException(403, "sign-in is restricted to an approved Google Workspace domain")


def _discovery(issuer: str) -> dict:
    """The provider's OIDC discovery document, cached per issuer."""
    with _lock:
        doc = _cache.get(("disc", issuer))
        if doc is None:
            url = f"{issuer}/.well-known/openid-configuration"
            doc = httpx.get(url, timeout=_HTTP_TIMEOUT).json()
            _cache[("disc", issuer)] = doc
        return doc


def _jwks(jwks_uri: str, *, force: bool = False) -> dict:
    """The provider's signing keys, cached with a TTL. `force` refetches, for key rotation."""
    with _lock:
        cached = _cache.get(("jwks", jwks_uri))
        if not force and cached and (time.time() - cached[1]) < _JWKS_TTL:
            return {"keys": cached[0]}
        keys = httpx.get(jwks_uri, timeout=_HTTP_TIMEOUT).json().get("keys", [])
        _cache[("jwks", jwks_uri)] = (keys, time.time())
        return {"keys": keys}


def _jwks_for_token(jwks_uri: str, kid: str | None) -> dict:
    """Cached keys, but refetch once when the token's kid is unknown (the provider rotated keys)."""
    jwks = _jwks(jwks_uri)
    if kid and kid not in {k.get("kid") for k in jwks["keys"]}:
        jwks = _jwks(jwks_uri, force=True)
    return jwks


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@router.get("/auth/login")
def login(request: Request):
    """Start the auth-code + PKCE flow: persist state/verifier/nonce, redirect to the provider."""
    cfg = _config()
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    request.session["oidc_tx"] = {"state": state, "nonce": nonce, "verifier": verifier}
    disc = _discovery(cfg["issuer"])
    params = {
        "response_type": "code", "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_url"], "scope": cfg["scope"],
        "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    return RedirectResponse(str(httpx.URL(disc["authorization_endpoint"], params=params)))


@router.get("/auth/callback")
def callback(request: Request, code: str | None = None, state: str | None = None,
             error: str | None = None, error_description: str | None = None):
    """Complete the flow: verify state, redeem the code, validate the ID token, set the session."""
    if error:
        raise HTTPException(401, f"the identity provider refused the login: {error_description or error}")
    tx = request.session.pop("oidc_tx", None)   # single-use: pop before we act on it
    if not tx or not code or not state or not secrets.compare_digest(state, tx["state"]):
        raise HTTPException(401, "login could not be verified (state mismatch or expired session); retry")

    cfg = _config()
    disc = _discovery(cfg["issuer"])
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": cfg["redirect_url"],
            "client_id": cfg["client_id"], "code_verifier": tx["verifier"]}
    if cfg["client_secret"]:
        data["client_secret"] = cfg["client_secret"]
    resp = httpx.post(disc["token_endpoint"], data=data, timeout=_HTTP_TIMEOUT)
    id_token = resp.json().get("id_token") if resp.status_code == 200 else None
    if not id_token:
        raise HTTPException(401, "the identity provider did not return an ID token")

    import jwt
    try:
        kid = jwt.get_unverified_header(id_token).get("kid")
    except jwt.PyJWTError:
        kid = None
    jwks = _jwks_for_token(disc["jwks_uri"], kid)
    try:
        claims = verify_id_token(id_token, jwks, issuer=disc["issuer"], audience=cfg["client_id"],
                                 nonce=tx["nonce"])
    except InvalidToken as e:
        raise HTTPException(401, f"the ID token failed validation: {e}")

    _check_domain(claims, cfg["allowed_domains"])
    request.session["user"] = identity_from_claims(claims, role_claim=cfg["role_claim"],
                                                   role_map=cfg["role_map"])
    return RedirectResponse("/", status_code=303)


@router.get("/auth/logout")
def logout(request: Request):
    """Clear the local session (does not initiate provider single-logout)."""
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@router.get("/auth/me")
def me(request: Request):
    """Who the session belongs to, for the UI to show the signed-in user and gate controls by role.
    A 401 here (from the app-wide gate) simply means not signed in."""
    user = request.session.get("user") or {}
    return {"authenticated": bool(user), "email": user.get("email", ""),
            "name": user.get("name", ""), "role": user.get("role", "")}
