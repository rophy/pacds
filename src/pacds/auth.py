"""Verify bearer JWTs from configured OIDC issuers (Kubernetes ServiceAccounts first)."""

from __future__ import annotations

import ssl
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import jwt

from pacds.config import IssuerConfig
from pacds.errors import PacdsError

ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "PS256"})

JwksFetcher = Callable[[IssuerConfig], Awaitable[dict[str, Any]]]


def _unauthorized() -> PacdsError:
    return PacdsError(401, "unauthorized", "invalid or missing bearer token")


async def fetch_jwks(issuer: IssuerConfig) -> dict[str, Any]:
    """Fetch an issuer's JWKS, via OIDC discovery unless `jwks_uri` is configured."""
    headers = {}
    if issuer.token_file:
        headers["Authorization"] = "Bearer " + Path(issuer.token_file).read_text().strip()
    verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=issuer.ca_file) if issuer.ca_file else True
    async with httpx.AsyncClient(verify=verify, headers=headers, timeout=10, trust_env=False) as client:
        uri = issuer.jwks_uri
        if uri is None:
            discovery = await client.get(issuer.issuer.rstrip("/") + "/.well-known/openid-configuration")
            discovery.raise_for_status()
            uri = discovery.json()["jwks_uri"]
        response = await client.get(uri)
        response.raise_for_status()
        return response.json()


class TokenVerifier:
    def __init__(
        self,
        issuers: list[IssuerConfig],
        fetch_jwks: JwksFetcher,
        *,
        ttl_seconds: float = 300.0,
        min_refresh_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._issuers = {issuer.issuer: issuer for issuer in issuers}
        self._fetch = fetch_jwks
        self._ttl = ttl_seconds
        self._min_refresh = min_refresh_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, jwt.PyJWKSet]] = {}

    async def verify(self, token: str) -> str:
        try:
            header = jwt.get_unverified_header(token)
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            raise _unauthorized() from None
        algorithm = header.get("alg")
        if algorithm not in ALLOWED_ALGORITHMS:
            raise _unauthorized()
        issuer = self._issuers.get(unverified.get("iss"))
        if issuer is None:
            raise _unauthorized()
        key = await self._key(issuer, header.get("kid"))
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=issuer.audience,
                issuer=issuer.issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError:
            raise _unauthorized() from None
        return claims["sub"]

    async def _key(self, issuer: IssuerConfig, kid: str | None) -> Any:
        key = _find(await self._keyset(issuer, refresh=False), kid)
        if key is None:
            key = _find(await self._keyset(issuer, refresh=True), kid)
        if key is None:
            raise _unauthorized()
        return key.key

    async def _keyset(self, issuer: IssuerConfig, *, refresh: bool) -> jwt.PyJWKSet:
        cached = self._cache.get(issuer.issuer)
        now = self._clock()
        if cached is not None:
            age = now - cached[0]
            if (not refresh and age < self._ttl) or (refresh and age < self._min_refresh):
                return cached[1]
        try:
            keyset = jwt.PyJWKSet.from_dict(await self._fetch(issuer))
        except Exception as error:
            raise PacdsError(502, "jwks_unavailable", "cannot fetch token signing keys") from error
        self._cache[issuer.issuer] = (now, keyset)
        return keyset


def _find(keyset: jwt.PyJWKSet, kid: str | None) -> jwt.PyJWK | None:
    for key in keyset.keys:
        if kid is None or key.key_id == kid:
            return key
    return None
