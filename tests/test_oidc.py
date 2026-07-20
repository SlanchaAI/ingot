"""OIDC ID-token validation, tested against forged tokens (no IdP, no network), layer 2 of the
SSO/RBAC test strategy. The `idp` fixture (tests/conftest.FakeIdp) mints RS256 tokens and exposes a
matching JWKS; `verify_id_token` is the primitive the OIDC callback will call."""
import base64
import hashlib
import hmac
import json
import time

import pytest

from ui.oidc import InvalidToken, verify_id_token


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _handcrafted_token(idp, alg: str, signer=None) -> str:
    """A token with an arbitrary `alg` header (PyJWT's encoder refuses to build these)."""
    now = int(time.time())
    header = _b64url(json.dumps({"alg": alg, "typ": "JWT", "kid": idp.kid}).encode())
    payload = _b64url(json.dumps({"iss": idp.issuer, "aud": idp.audience, "sub": "mallory",
                                  "iat": now, "exp": now + 300}).encode())
    signing_input = f"{header}.{payload}"
    return f"{signing_input}.{signer(signing_input) if signer else ''}"


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
    aud = [idp.audience, "other-app"]
    without_azp = idp.id_token(aud=aud)
    with pytest.raises(InvalidToken):
        verify_id_token(without_azp, idp.jwks, idp.issuer, idp.audience)
    wrong_azp = idp.id_token(aud=aud, azp="other-app")
    with pytest.raises(InvalidToken):
        verify_id_token(wrong_azp, idp.jwks, idp.issuer, idp.audience)
    with_azp = idp.id_token(aud=aud, azp=idp.audience)
    assert verify_id_token(with_azp, idp.jwks, idp.issuer, idp.audience)["azp"] == idp.audience


def test_mismatched_azp_is_rejected_even_single_audience(idp):
    # OIDC core: if azp is present it must be our client_id, a wrong azp means the token was
    # issued to a different client, regardless of aud shape
    token = idp.id_token(azp="other-app")
    with pytest.raises(InvalidToken):
        verify_id_token(token, idp.jwks, idp.issuer, idp.audience)


def test_garbage_tokens_are_rejected_not_500(idp):
    for garbage in ("", "not-a-jwt", "a.b", "a.b.c", "ey.ey.sig"):
        with pytest.raises(InvalidToken):
            verify_id_token(garbage, idp.jwks, idp.issuer, idp.audience)


def test_alg_none_token_is_rejected(idp):
    # unsigned token claiming alg=none must not pass just because its kid resolves
    token = _handcrafted_token(idp, "none")
    with pytest.raises(InvalidToken):
        verify_id_token(token, idp.jwks, idp.issuer, idp.audience)


def test_hs256_alg_confusion_is_rejected(idp):
    # classic key-confusion attack: HMAC-sign with the server's own PUBLIC key as the shared
    # secret; if the verifier honored the header's alg, the signature would check out
    from cryptography.hazmat.primitives import serialization
    pub_pem = idp._key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    token = _handcrafted_token(
        idp, "HS256",
        signer=lambda si: _b64url(hmac.new(pub_pem, si.encode(), hashlib.sha256).digest()))
    with pytest.raises(InvalidToken):
        verify_id_token(token, idp.jwks, idp.issuer, idp.audience)


@pytest.mark.parametrize("missing", ["exp", "iat", "aud", "iss"])
def test_missing_required_claim_is_rejected(idp, missing):
    import jwt as pyjwt
    now = int(time.time())
    payload = {"iss": idp.issuer, "aud": idp.audience, "sub": "u", "iat": now, "exp": now + 300}
    del payload[missing]
    token = pyjwt.encode(payload, idp._key, algorithm="RS256", headers={"kid": idp.kid})
    with pytest.raises(InvalidToken):
        verify_id_token(token, idp.jwks, idp.issuer, idp.audience)


def test_future_iat_is_rejected(idp):
    # pins the pyjwt>=2.10 floor in requirements.txt: older versions accepted a not-yet-issued
    # token, which would let a pre-dated forgery ride out a key rotation
    token = idp.id_token(iat=int(time.time()) + 3600)
    with pytest.raises(InvalidToken):
        verify_id_token(token, idp.jwks, idp.issuer, idp.audience)


def test_expiry_within_leeway_is_tolerated(idp):
    # the default 60s leeway absorbs small clock skew between us and the IdP
    token = idp.id_token(exp_delta=-30)
    assert verify_id_token(token, idp.jwks, idp.issuer, idp.audience)["sub"] == "user-1"


def test_empty_jwks_is_rejected(idp):
    with pytest.raises(InvalidToken):
        verify_id_token(idp.id_token(), {"keys": []}, idp.issuer, idp.audience)
    with pytest.raises(InvalidToken):
        verify_id_token(idp.id_token(), {}, idp.issuer, idp.audience)


def test_token_without_kid_header_is_rejected(idp):
    import jwt as pyjwt
    now = int(time.time())
    token = pyjwt.encode(
        {"iss": idp.issuer, "aud": idp.audience, "sub": "u", "iat": now, "exp": now + 300},
        idp._key, algorithm="RS256")   # no kid header -> nothing to match in the JWKS
    with pytest.raises(InvalidToken):
        verify_id_token(token, idp.jwks, idp.issuer, idp.audience)


def test_single_element_audience_list_needs_no_azp(idp):
    # the azp requirement kicks in only for genuinely multi-audience tokens
    token = idp.id_token(aud=[idp.audience])
    assert verify_id_token(token, idp.jwks, idp.issuer, idp.audience)["sub"] == "user-1"


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


def test_verify_then_map_okta_groups_end_to_end(idp):
    """Same pipeline through the Okta shape: role values in a `groups` claim."""
    from ui.rbac import identity_from_claims
    token = idp.id_token(sub="carol", email="carol@corp.com", groups=["all-staff", "eng-admins"])
    claims = verify_id_token(token, idp.jwks, idp.issuer, idp.audience)
    identity = identity_from_claims(claims, role_claim="groups",
                                    role_map={"eng-admins": "admin"})
    assert identity["role"] == "admin" and identity["email"] == "carol@corp.com"
