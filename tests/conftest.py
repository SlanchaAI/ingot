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
def _isolated_state(tmp_path_factory, monkeypatch):
    """No test may touch the developer's real state directory, or the checkout's.

    State resolves through `ingot.paths` at call time and defaults to an XDG directory, so a test
    that reaches a default instead of a fixture would write a real review queue and real receipts
    into `~/.local/state/ingot` and pass while doing it. One per-test INGOT_HOME contains every one
    of them; specific overrides are cleared so an environment variable the developer happens to
    have exported cannot reach in either.

    This also replaces the old SKILLS_DIR patch. `configured_roots` always puts the local authoring
    root first, even ahead of an explicit root, so a test that loads skills would otherwise see
    whatever `scripts/fetch_skills.sh` left in the checkout (first caught on a machine with 72
    fetched skills: 9 failures and a multi-minute embedding stall)."""
    from ingot import paths
    monkeypatch.setenv(paths.HOME, str(tmp_path_factory.mktemp("state")))
    for name in (paths.LIBRARY, paths.RUNS, paths.TASKS, paths.VAULT, *paths.LEGACY.values()):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SKILL_ROUTER_PATHS", raising=False)


class FakeIdp:
    """Forge RS256 ID tokens against an in-memory JWKS, the layer-2 harness for OIDC validation
    tests. No IdP, no network: one keypair,
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
