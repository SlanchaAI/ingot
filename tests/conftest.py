"""Shared test isolation. `configured_roots` always puts the local authoring root (SKILLS_DIR)
first, even ahead of explicit roots, so any test that loads skills would also see whatever
`scripts/fetch_skills.sh` has put in ./skills (first caught on a checkout with 72 fetched skills:
9 failures + a multi-minute embedding stall). Point SKILLS_DIR at an empty per-test directory and
clear SKILL_ROUTER_PATHS so the suite is hermetic; tests that care about the local root patch it
themselves on top of this."""
import json
import time

import pytest


@pytest.fixture(autouse=True)
def _isolated_local_skills_root(tmp_path_factory, monkeypatch):
    monkeypatch.setattr("mcp_server.registry.SKILLS_DIR", tmp_path_factory.mktemp("local-skills"))
    monkeypatch.delenv("SKILL_ROUTER_PATHS", raising=False)


class FakeIdp:
    """Forge RS256 ID tokens against an in-memory JWKS, the layer-2 harness for OIDC validation
    tests (docs/superpowers/specs/2026-07-19-sso-rbac-design.md). No IdP, no network: one keypair,
    mint tokens with any claims/overrides (expiry, aud, iss, kid, or a different signing key), and
    expose the matching JWKS to feed `ui.oidc.verify_id_token`."""

    def __init__(self, issuer="https://idp.test/", audience="ingot", kid="test-kid"):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        self._jwt = jwt
        self.issuer, self.audience, self.kid = issuer, audience, kid
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @property
    def jwks(self) -> dict:
        jwk = json.loads(self._jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key()))
        return {"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def id_token(self, sub="user-1", *, exp_delta=300, iss=None, aud=None, kid=None, key=None,
                 **claims) -> str:
        now = int(time.time())
        payload = {"iss": iss or self.issuer, "aud": aud or self.audience, "sub": sub,
                   "iat": now, "exp": now + exp_delta, **claims}
        return self._jwt.encode(payload, key or self._key, algorithm="RS256",
                                headers={"kid": kid or self.kid})


@pytest.fixture
def idp():
    return FakeIdp()
