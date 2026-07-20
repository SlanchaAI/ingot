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
    if env and secrets.compare_digest(user, env[0]) and secrets.compare_digest(password, env[1]):
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


def require_auth(request: Request) -> None:
    """App-wide gate: reject a request with missing/invalid credentials when auth is enabled."""
    if _actor_from(request) is None:
        raise _challenge()


def current_actor(request: Request) -> str:
    """The username to attribute a state-changing action to (`_ANON` when auth is disabled)."""
    actor = _actor_from(request)
    if actor is None:
        raise _challenge()
    return actor


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
