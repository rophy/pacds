"""Bearer tokens for calling PACDS: the dev stack's mock OIDC provider, or any OIDC issuer (TokenSource).

TokenSource picks, in order:
  PACDS_TOKEN                          a static bearer token (short runs, manual tests)
  PACDS_OIDC_TOKEN_URL + CLIENT_ID + CLIENT_SECRET   the client-credentials grant against the corporate issuer;
                                       optional PACDS_OIDC_SCOPE and PACDS_OIDC_AUDIENCE (issuers that need one)
  otherwise                            the dev stack's mock provider (compose.yaml, dev/oidc-mock.yaml)
Tokens from the client-credentials grant are refreshed before they expire, so a run of hours keeps working.
TLS to the issuer trusts SSL_CERT_FILE when set (e.g. the corporate CA bundle).
"""

import os
import re
import threading
import time
from collections.abc import Callable, Mapping

import httpx

OIDC_URL = os.environ.get("PACDS_OIDC_URL", "http://localhost:3003")
SUBJECT = "triage-agent"
REDIRECT_URI = "http://localhost/callback"


def token(audience: str = "pacds", subject: str = SUBJECT) -> str:
    # oidc-mock only has the authorization code grant: pick the user non-interactively, then exchange the code.
    # An access token's aud is the client_id, so the audience names the client.
    with httpx.Client(base_url=OIDC_URL, timeout=10, trust_env=False) as client:
        picked = client.post(
            "/authorize/callback",
            data={"sub": subject, "client_id": audience, "redirect_uri": REDIRECT_URI, "scope": "openid"},
        )
        # Success is a redirect to the callback carrying the code.
        code = re.search(r"[?&]code=([^&]+)", picked.headers.get("location", ""))
        if picked.status_code != 302 or code is None:
            raise RuntimeError(f"oidc-mock authorize failed: {picked.status_code} {picked.text[:200]}")
        response = client.post(
            "/token",
            data={"grant_type": "authorization_code", "code": code.group(1), "redirect_uri": REDIRECT_URI, "client_id": audience, "client_secret": "dev"},
        )
        response.raise_for_status()
        return response.json()["access_token"]


# Refresh this long before a token's expiry, so a request never leaves with a token about to lapse.
REFRESH_MARGIN_SECONDS = 60


class TokenSource:
    def __init__(self, env: Mapping[str, str] = os.environ, *, clock: Callable[[], float] = time.monotonic,
                 http: httpx.Client | None = None) -> None:
        self._env, self._clock, self._http = env, clock, http
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires = 0.0

    @property
    def kind(self) -> str:
        if self._env.get("PACDS_TOKEN"):
            return "static"
        return "client_credentials" if self._env.get("PACDS_OIDC_TOKEN_URL") else "dev-mock"

    def get(self) -> str:
        if self.kind == "static":
            return self._env["PACDS_TOKEN"]
        with self._lock:
            if self._token is None or self._clock() >= self._expires - REFRESH_MARGIN_SECONDS:
                self._token, lifetime = self._client_credentials() if self.kind == "client_credentials" else (token(), 300.0)
                self._expires = self._clock() + lifetime
            return self._token

    def _client_credentials(self) -> tuple[str, float]:
        data = {"grant_type": "client_credentials", "client_id": self._env["PACDS_OIDC_CLIENT_ID"],
                "client_secret": self._env["PACDS_OIDC_CLIENT_SECRET"]}
        for name, field in (("PACDS_OIDC_SCOPE", "scope"), ("PACDS_OIDC_AUDIENCE", "audience")):
            if self._env.get(name):
                data[field] = self._env[name]
        http = self._http or httpx.Client(timeout=30)
        try:
            response = http.post(self._env["PACDS_OIDC_TOKEN_URL"], data=data)
        finally:
            if self._http is None:
                http.close()
        if response.status_code != 200:
            raise RuntimeError(f"token request failed: {response.status_code} {response.text[:200]}")
        body = response.json()
        return body["access_token"], float(body.get("expires_in") or 300)
