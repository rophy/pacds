"""Dev client tokens from the Compose stack's mock OIDC provider (compose.yaml, dev/oidc-mock.yaml)."""

import os
import re

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
