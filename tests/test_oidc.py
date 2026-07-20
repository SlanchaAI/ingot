"""OIDC ID-token validation, tested against forged tokens (no IdP, no network), layer 2 of the
SSO/RBAC test strategy. The `idp` fixture (tests/conftest.FakeIdp) mints RS256 tokens and exposes a
matching JWKS; `verify_id_token` is the primitive the OIDC callback will call."""
import pytest

from ui.oidc import InvalidToken, verify_id_token


def test_valid_token_returns_claims(idp):
    token = idp.id_token(sub="alice", email="alice@corp.com", roles=["ingot-admin"])
    claims = verify_id_token(token, idp.jwks, idp.issuer, idp.audience)
    assert claims["sub"] == "alice" and claims["email"] == "alice@corp.com"
    assert claims["roles"] == ["ingot-admin"]


def test_expired_token_is_rejected(idp):
    token = idp.id_token(exp_delta=-3600)   # well past any leeway
    with pytest.raises(InvalidToken):
        verify_id_token(token, idp.jwks, idp.issuer, idp.audience)


def test_wrong_audience_is_rejected(idp):
    token = idp.id_token(aud="some-other-app")
    with pytest.raises(InvalidToken):
        verify_id_token(token, idp.jwks, idp.issuer, idp.audience)


def test_wrong_issuer_is_rejected(idp):
    token = idp.id_token(iss="https://evil.example/")
    with pytest.raises(InvalidToken):
        verify_id_token(token, idp.jwks, idp.issuer, idp.audience)


def test_bad_signature_is_rejected(idp):
    # a different IdP with the SAME kid → the token's kid resolves to the wrong public key
    from tests.conftest import FakeIdp
    attacker = FakeIdp(issuer=idp.issuer, audience=idp.audience, kid=idp.kid)
    token = idp.id_token()                                   # signed by the real key
    with pytest.raises(InvalidToken):
        verify_id_token(token, attacker.jwks, idp.issuer, idp.audience)


def test_unknown_kid_is_rejected(idp):
    token = idp.id_token(kid="not-in-jwks")
    with pytest.raises(InvalidToken):
        verify_id_token(token, idp.jwks, idp.issuer, idp.audience)


def test_nonce_must_match_when_required(idp):
    token = idp.id_token(nonce="n-123")
    assert verify_id_token(token, idp.jwks, idp.issuer, idp.audience, nonce="n-123")["nonce"] == "n-123"
    with pytest.raises(InvalidToken):
        verify_id_token(token, idp.jwks, idp.issuer, idp.audience, nonce="wrong")


def test_token_without_sub_is_rejected(idp):
    import time

    import jwt as pyjwt
    now = int(time.time())
    token = pyjwt.encode({"iss": idp.issuer, "aud": idp.audience, "iat": now, "exp": now + 300},
                         idp._key, algorithm="RS256", headers={"kid": idp.kid})
    with pytest.raises(InvalidToken):
        verify_id_token(token, idp.jwks, idp.issuer, idp.audience)


def test_multi_audience_requires_matching_azp(idp):
    without_azp = idp.id_token(aud=["ingot", "other-app"])
    with pytest.raises(InvalidToken):
        verify_id_token(without_azp, idp.jwks, idp.issuer, idp.audience)
    with_azp = idp.id_token(aud=["ingot", "other-app"], azp="ingot")
    assert verify_id_token(with_azp, idp.jwks, idp.issuer, idp.audience)["azp"] == "ingot"


def test_malformed_jwks_key_raises_invalid_token_not_500(idp):
    bad_jwks = {"keys": [{"kid": idp.kid, "kty": "RSA", "n": "!!!not-base64!!!", "e": "AQAB"}]}
    with pytest.raises(InvalidToken):
        verify_id_token(idp.id_token(), bad_jwks, idp.issuer, idp.audience)


def test_verify_then_map_to_role_end_to_end(idp):
    """The full identity pipeline minus the browser flow: forge -> verify -> RBAC identity."""
    from ui.rbac import identity_from_claims
    token = idp.id_token(sub="bob", email="bob@corp.com", roles=["ingot-approver"])
    claims = verify_id_token(token, idp.jwks, idp.issuer, idp.audience)
    identity = identity_from_claims(claims, role_claim="roles",
                                    role_map={"ingot-approver": "approver"})
    assert identity == {"sub": "bob", "email": "bob@corp.com", "name": "", "role": "approver"}
