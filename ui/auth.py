"""Minimal password auth for the change-control UI, for a shared server on a trusted company LAN.

Deliberately small: HTTP Basic against a local users file of salted PBKDF2 hashes, so the browser
prompts once and every request (including an approval) carries the username. That username becomes
the audit-trail `actor`, which is the whole point, on a shared server an approval has to be
attributable to a person, not to `local-operator`.

Opt-in: with no users file the UI stays open (the zero-config local default is unchanged). Create
the first user to turn auth on:

    python -m ui.auth add alice        # prompts for a password, writes runs/auth.json (mode 0600)

This is LAN-grade, not internet-grade: Basic credentials ride every request, so put the server
behind your network boundary (and TLS if you have it). For SSO / RBAC / per-team policy, that's the
enterprise profile, not this.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Request

AUTH_FILE = Path(os.environ.get("AUTH_FILE") or Path(__file__).resolve().parent.parent / "runs" / "auth.json")
_ITERATIONS = 200_000
_ANON = "local-operator"   # actor when auth is disabled, matches the pre-auth default
# The compose default (docker-compose.yml sets AUTH_PASSWORD=${AUTH_PASSWORD:-ingot}); we warn while
# it is unchanged so nobody exposes the UI on it. Documented in .env.example and the README.
DEFAULT_PASSWORD = "ingot"


def _env_user() -> tuple[str, str] | None:
    """A single user from AUTH_USER / AUTH_PASSWORD (both must be set), the compose-friendly path."""
    user, password = os.environ.get("AUTH_USER"), os.environ.get("AUTH_PASSWORD")
    return (user, password) if user and password else None


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS).hex()
    return f"pbkdf2_sha256${_ITERATIONS}${salt}${dk}"


def _verify(password: str, stored: str) -> bool:
    try:
        _algo, iters, salt, dk = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters)).hex()
        return hmac.compare_digest(got, dk)
    except (ValueError, AttributeError):
        return False


def load_users() -> dict[str, str]:
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def auth_enabled() -> bool:
    """Auth turns on as soon as an AUTH_USER/AUTH_PASSWORD pair or a users file exists; otherwise the
    UI stays open (local default)."""
    return _env_user() is not None or bool(load_users())


def _valid(user: str, password: str) -> bool:
    env = _env_user()
    # compare bytes: compare_digest raises TypeError on non-ASCII str, which would turn a stray
    # UTF-8 credential into a 500 instead of a 401
    if env and (secrets.compare_digest(user.encode(), env[0].encode())
                and secrets.compare_digest(password.encode(), env[1].encode())):
        return True
    stored = load_users().get(user)
    return bool(stored and _verify(password, stored))


def using_default_password() -> bool:
    env = _env_user()
    return bool(env and env[1] == DEFAULT_PASSWORD)


def _actor_from(request: Request) -> str | None:
    """The authenticated username, `_ANON` when auth is off, or None when creds are missing/invalid."""
    if not auth_enabled():
        return _ANON
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return None
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    return user if _valid(user, password) else None


def _challenge() -> HTTPException:
    return HTTPException(status_code=401, detail="authentication required",
                         headers={"WWW-Authenticate": 'Basic realm="ingot"'})


# --- Auth mode selection ---------------------------------------------------------------------
# Three modes coexist: `oidc` (Sign in with Google, ui/oidc_flow.py), `password` (LAN Basic, above),
# and `open` (the zero-config local default). Explicit AUTH_MODE wins; otherwise infer password when
# credentials exist, else open. OIDC is opt-in via AUTH_MODE=oidc because it needs the OIDC_* config.
_OIDC_BOOTSTRAP = ("/auth/login", "/auth/callback", "/auth/logout")
_REQUIRED_OIDC_ENV = ("OIDC_CLIENT_ID", "OIDC_REDIRECT_URL", "SESSION_SECRET")


def auth_mode() -> str:
    """`oidc` | `password` | `open`."""
    mode = (os.environ.get("AUTH_MODE") or "").strip().lower()
    if mode in ("oidc", "password", "open"):
        return mode
    return "password" if (_env_user() or load_users()) else "open"


def validate_oidc_config() -> None:
    """Fail closed at startup: AUTH_MODE=oidc must have its config, never silently downgrade to LAN
    password or open. Called from ui/app.py before the middleware/routes are wired."""
    missing = [k for k in _REQUIRED_OIDC_ENV if not (os.environ.get(k) or "").strip()]
    if missing:
        raise RuntimeError(f"AUTH_MODE=oidc is missing {', '.join(missing)}; refusing to start "
                           "(it must not fall back to password/open). See docs/sso.md.")
    if len((os.environ.get("SESSION_SECRET") or "").strip()) < 16:
        raise RuntimeError("SESSION_SECRET must be at least 16 characters; refusing to start. "
                           "Generate one with `python -c \"import secrets; print(secrets.token_urlsafe(32))\"`.")


def oidc_cookie_kwargs() -> dict:
    """SessionMiddleware settings for the signed session cookie. `Secure` follows the redirect URL's
    scheme so local http still works while production https gets the flag; an explicit max-age means
    a stolen cookie expires (phase 1 re-logs in rather than refreshing)."""
    https = (os.environ.get("OIDC_REDIRECT_URL") or "").lower().startswith("https")
    max_age = int(os.environ.get("SESSION_MAX_AGE") or 8 * 3600)
    return {"same_site": "lax", "https_only": https, "max_age": max_age}


def require_auth(request: Request) -> None:
    """App-wide gate. OIDC mode: allow the bootstrap routes (`/auth/*`) and the index (which
    redirects to login itself), require a session everywhere else. Password/open mode: unchanged."""
    if auth_mode() == "oidc":
        if request.url.path in _OIDC_BOOTSTRAP or request.url.path == "/":
            return
        if not request.session.get("user"):
            raise HTTPException(401, "authentication required", headers={"Location": "/auth/login"})
        return
    if _actor_from(request) is None:
        raise _challenge()


def current_actor(request: Request) -> str:
    """The identity to attribute a state-changing action to: the SSO email/sub in OIDC mode, the
    Basic username in password mode, `_ANON` when auth is disabled."""
    if auth_mode() == "oidc":
        user = request.session.get("user")
        if not user:
            raise HTTPException(401, "authentication required")
        return user.get("email") or user.get("sub") or "sso-user"
    actor = _actor_from(request)
    if actor is None:
        raise _challenge()
    return actor


def require_role(role: str):
    """Dependency factory gating an endpoint by app role. RBAC is the SSO profile: in password/open
    mode (single trust domain) it is a no-op, so the LAN/local behavior is unchanged. Composed
    *alongside* `same_origin`, never replacing it."""
    from ui.rbac import authorize

    def dep(request: Request) -> None:
        if auth_mode() != "oidc":
            return
        user = request.session.get("user")
        if not user:
            raise HTTPException(401, "authentication required")
        authorize(user.get("role", "viewer"), role)

    return dep


def _add_user_cli() -> None:
    import getpass
    import sys
    if len(sys.argv) < 3 or sys.argv[1] != "add":
        print("usage: python -m ui.auth add <username>")
        raise SystemExit(2)
    username = sys.argv[2]
    password = getpass.getpass(f"password for {username}: ")
    if not password:
        print("empty password, aborted")
        raise SystemExit(1)
    users = load_users()
    users[username] = hash_password(password)
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")
    os.chmod(AUTH_FILE, 0o600)
    print(f"user '{username}' saved to {AUTH_FILE}, auth is now ON for the UI")


if __name__ == "__main__":
    _add_user_cli()
