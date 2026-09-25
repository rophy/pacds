import os
import subprocess
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://issuer.test"
AUDIENCE = "pacds"
SUBJECT = "system:serviceaccount:support:triage-agent"


@pytest.fixture(scope="session")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def jwks(signing_key):
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    jwk.update({"kid": "k1", "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


@pytest.fixture(scope="session")
def make_token(signing_key):
    def make(claims: dict | None = None, *, key=None, kid: str = "k1", algorithm: str = "RS256") -> str:
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": SUBJECT, "iat": now, "exp": now + 600}
        payload.update(claims or {})
        payload = {name: value for name, value in payload.items() if value is not None}
        return jwt.encode(payload, signing_key if key is None else key, algorithm=algorithm, headers={"kid": kid})

    return make


def _git(*args: str, cwd: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    result = subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def origin(tmp_path) -> tuple[str, str]:
    """A bare git repo reachable by file:// URL; returns (url, head_sha)."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "app.py").write_text("def checkout(cart):\n    raise ValueError('payment declined')\n")
    (work / "src").mkdir()
    (work / "src" / "util.py").write_text("TIMEOUT = 30\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "init", cwd=work)
    _git("tag", "v1", cwd=work)
    sha = _git("rev-parse", "HEAD", cwd=work)
    bare = tmp_path / "origin.git"
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
    _git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=bare)
    return bare.as_uri(), sha
