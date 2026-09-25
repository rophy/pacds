import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from pacds.auth import TokenVerifier
from pacds.config import IssuerConfig
from pacds.errors import PacdsError
from tests.conftest import AUDIENCE, ISSUER, SUBJECT

ISSUERS = [IssuerConfig(issuer=ISSUER, audience=AUDIENCE)]


def verifier_for(jwks, calls: list | None = None, clock=None) -> TokenVerifier:
    async def fetch(issuer):
        if calls is not None:
            calls.append(issuer.issuer)
        return jwks

    kwargs = {"clock": clock} if clock else {}
    return TokenVerifier(ISSUERS, fetch, **kwargs)


async def test_valid_token_returns_subject(jwks, make_token):
    assert await verifier_for(jwks).verify(make_token()) == SUBJECT


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": 1},
        {"aud": "kubernetes"},
        {"iss": "https://other.test"},
        {"sub": None},
    ],
)
async def test_bad_claims_are_rejected(jwks, make_token, claims):
    with pytest.raises(PacdsError) as error:
        await verifier_for(jwks).verify(make_token(claims))
    assert error.value.status == 401


async def test_wrong_signature_is_rejected(jwks, make_token):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(PacdsError) as error:
        await verifier_for(jwks).verify(make_token(key=other))
    assert error.value.status == 401


async def test_alg_none_and_hmac_are_rejected(jwks, make_token):
    for token in (make_token(key="", algorithm="none"), make_token(key="secret-secret-secret-secret-32b!", algorithm="HS256")):
        with pytest.raises(PacdsError) as error:
            await verifier_for(jwks).verify(token)
        assert error.value.status == 401


async def test_garbage_token_is_rejected(jwks):
    with pytest.raises(PacdsError) as error:
        await verifier_for(jwks).verify("not-a-jwt")
    assert error.value.status == 401


async def test_keys_are_cached(jwks, make_token):
    calls: list = []
    verifier = verifier_for(jwks, calls)
    await verifier.verify(make_token())
    await verifier.verify(make_token())
    assert calls == [ISSUER]


async def test_unknown_kid_refreshes_at_most_once_per_interval(jwks, make_token):
    calls: list = []
    now = [1000.0]
    verifier = verifier_for(jwks, calls, clock=lambda: now[0])
    await verifier.verify(make_token())
    for _ in range(3):
        with pytest.raises(PacdsError):
            await verifier.verify(make_token(kid="rotated"))
    assert len(calls) == 1
    now[0] += 31
    with pytest.raises(PacdsError):
        await verifier.verify(make_token(kid="rotated"))
    assert len(calls) == 2


async def test_jwks_fetch_failure_is_502(make_token):
    async def failing(issuer):
        raise OSError("down")

    with pytest.raises(PacdsError) as error:
        await TokenVerifier(ISSUERS, failing).verify(make_token())
    assert error.value.status == 502
