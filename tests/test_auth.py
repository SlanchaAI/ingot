"""Minimal LAN password auth: hashing, the open-by-default behavior, the enabled gate, and that an
approval is attributed to the authenticated user in the audit trail."""
import base64
import json

from fastapi.testclient import TestClient

from ui import auth
from ui.app import app


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_hash_and_verify_roundtrip():
    h = auth.hash_password("s3cret")
    assert h.startswith("pbkdf2_sha256$") and auth._verify("s3cret", h)
    assert not auth._verify("wrong", h)


def test_open_when_no_users_file_and_no_env(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    assert not auth.auth_enabled()
    assert TestClient(app).get("/api/config").status_code == 200   # no credentials needed


def test_env_credentials_gate_the_ui(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "auth.json")   # no users file; env creds only
    monkeypatch.setenv("AUTH_USER", "admin")
    monkeypatch.setenv("AUTH_PASSWORD", "s3cret")
    assert auth.auth_enabled()
    c = TestClient(app)
    assert c.get("/api/config").status_code == 401
    assert c.get("/api/config", headers=_basic("admin", "nope")).status_code == 401
    assert c.get("/api/config", headers=_basic("admin", "s3cret")).status_code == 200


def test_empty_password_env_means_open(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setenv("AUTH_USER", "admin")
    monkeypatch.setenv("AUTH_PASSWORD", "")          # the documented "run open" escape hatch
    assert not auth.auth_enabled()
    assert TestClient(app).get("/api/config").status_code == 200


def test_using_default_password_flag(monkeypatch):
    monkeypatch.setenv("AUTH_USER", "admin")
    monkeypatch.setenv("AUTH_PASSWORD", auth.DEFAULT_PASSWORD)
    assert auth.using_default_password()
    monkeypatch.setenv("AUTH_PASSWORD", "changed")
    assert not auth.using_default_password()


def test_enabled_gate_requires_valid_credentials(tmp_path, monkeypatch):
    f = tmp_path / "auth.json"
    f.write_text(json.dumps({"alice": auth.hash_password("pw")}))
    monkeypatch.setattr(auth, "AUTH_FILE", f)
    c = TestClient(app)
    unauth = c.get("/api/config")
    assert unauth.status_code == 401 and "Basic" in unauth.headers.get("www-authenticate", "")
    assert c.get("/api/config", headers=_basic("alice", "wrong")).status_code == 401
    assert c.get("/api/config", headers=_basic("bob", "pw")).status_code == 401       # unknown user
    assert c.get("/api/config", headers=_basic("alice", "pw")).status_code == 200


def test_actor_extraction_feeds_the_audit_attribution(tmp_path, monkeypatch):
    # current_actor / _actor_from resolve the username the approve + rollback endpoints pass as the
    # audit `actor`, so an approval is attributable to a person instead of "local-operator".
    f = tmp_path / "auth.json"
    f.write_text(json.dumps({"alice": auth.hash_password("pw")}))
    monkeypatch.setattr(auth, "AUTH_FILE", f)

    class Req:  # minimal stand-in carrying .headers
        def __init__(self, headers):
            self.headers = headers

    assert auth._actor_from(Req(_basic("alice", "pw"))) == "alice"
    assert auth._actor_from(Req(_basic("alice", "wrong"))) is None
    assert auth._actor_from(Req({})) is None                      # no credentials
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "absent.json")
    assert auth._actor_from(Req({})) == "local-operator"         # disabled -> the pre-auth default
