"""Smoke tests against the Kind dev cluster (skaffold dev --kube-context kind-pacds)."""

import json
import os
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest
from typesafe_sdk import (
    Choice,
    Noul,
    RetryPolicy,
    TypeSafeAuthenticationError,
    TypeSafeClient,
    TypeSafePermissionDeniedError,
    TypeSafeUnprocessableEntityError,
)

from tests.s3 import S3_HOST, dev_credentials, presign

pytestmark = pytest.mark.e2e

BASE_URL = os.environ.get("PACDS_URL", "http://localhost:3002")
CONTEXT = "kind-pacds"
GIT = {"url": "https://github.com/rophy/tostada.git", "ref": "main"}
CAUSES = {
    "code_defect": "A bug in the application code",
    "user_action": "Invalid input or wrong sequence of steps",
    "user_environment": "User's browser, device, network or local settings",
    "service_environment": "Infrastructure, deployment config or external dependency",
}


def token(audience: str = "pacds") -> str:
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, "-n", "support", "create", "token", "triage-agent", "--audience", audience],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def client(api_key: str | None = None) -> TypeSafeClient:
    return TypeSafeClient(api_key=api_key or token(), base_url=BASE_URL, timeout=300, retry=RetryPolicy(max_retries=0))


def log_url(key: str) -> str:
    access_key, secret_key = dev_credentials(CONTEXT)
    return presign(key, access_key=access_key, secret_key=secret_key)


def pacds_logs(since: str = "10m") -> str:
    return subprocess.run(
        ["kubectl", "--context", CONTEXT, "-n", "pacds", "logs", "deploy/pacds", f"--since={since}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_triage_with_repo_and_logs():
    url = log_url("e2e/checkout.log")
    response = client().system_one(
        state={"pacds": {"git": GIT, "logs": [{"name": "checkout.log", "url": url}]}, "user_report": "I cannot log in as alice; the page says user not found."},
        questions={"cause": Choice(instructions="What caused this issue?", criteria=CAUSES), "is_code": Noul(instructions="Is this issue caused by code?")},
    )
    assert response.answers["cause"].choice in CAUSES
    assert 0 <= response.answers["is_code"].noul <= 1
    # The presigned signature is a credential: it must never reach PACDS's logs or audit records.
    logs = pacds_logs()
    assert parse_qs(urlsplit(url).query)["X-Amz-Signature"][0] not in logs
    audits = [json.loads(line) for line in logs.splitlines() if '"pacds.audit"' in line]
    assert [S3_HOST] in [audit["log_hosts"] for audit in audits]


def test_models():
    assert [model.name for model in client().models.list().models] == ["pacds-1"]


def test_wrong_audience_is_rejected():
    with pytest.raises(TypeSafeAuthenticationError):
        client(token(audience="not-pacds")).system_one(state={"pacds": {"git": GIT}}, questions={"q": Noul(instructions="?")})


def test_repo_outside_allow_list_is_forbidden():
    with pytest.raises(TypeSafePermissionDeniedError):
        client().system_one(
            state={"pacds": {"git": {"url": "https://github.com/torvalds/linux.git", "ref": "master"}}},
            questions={"q": Noul(instructions="?")},
        )


@pytest.mark.parametrize(
    "url",
    ["http://169.254.169.254/latest/meta-data/", "https://evil.example.com/x.log", "http://kubernetes.default.svc/api"],
)
def test_log_urls_outside_allow_list_are_refused(url):
    with pytest.raises(TypeSafeUnprocessableEntityError):
        client().system_one(
            state={"pacds": {"git": GIT, "logs": [{"name": "x.log", "url": url}]}},
            questions={"q": Noul(instructions="?")},
        )
