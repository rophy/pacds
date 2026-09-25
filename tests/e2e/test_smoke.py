"""Smoke tests against the Kind dev cluster (skaffold dev --kube-context kind-pacds)."""

import os
import subprocess

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

pytestmark = pytest.mark.e2e

BASE_URL = os.environ.get("PACDS_URL", "http://localhost:3002")
CONTEXT = "kind-pacds"
GIT = {"url": "https://github.com/rophy/tostada.git", "ref": "main"}
LOG = {"name": "checkout.log", "url": "http://pacds-logs.pacds.svc.cluster.local:8000/checkout.log"}
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


def test_triage_with_repo_and_logs():
    response = client().system_one(
        state={"pacds": {"git": GIT, "logs": [LOG]}, "user_report": "I cannot log in as alice; the page says user not found."},
        questions={"cause": Choice(instructions="What caused this issue?", criteria=CAUSES), "is_code": Noul(instructions="Is this issue caused by code?")},
    )
    assert response.answers["cause"].choice in CAUSES
    assert 0 <= response.answers["is_code"].noul <= 1


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
