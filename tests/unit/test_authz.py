import pytest

from pacds.authz import is_authorized, normalize_git_url
from pacds.config import ClientConfig

SUBJECT = "system:serviceaccount:support:triage-agent"
CLIENTS = [ClientConfig(subject=SUBJECT, repos=["git.example.com/shop/*", "git.example.com/platform/**"])]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://git.example.com/shop/checkout.git", "git.example.com/shop/checkout"),
        ("https://GIT.example.com/shop/checkout", "git.example.com/shop/checkout"),
        ("https://git.example.com:8443/shop/checkout.git", "git.example.com:8443/shop/checkout"),
    ],
)
def test_normalize(url, expected):
    assert normalize_git_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://git.example.com/shop/checkout.git",
        "ssh://git@git.example.com/shop/checkout.git",
        "https://user:token@git.example.com/shop/checkout.git",
        "https://git.example.com/shop/../admin/secret.git",
        "https://git.example.com/shop/checkout.git?x=1",
        "https://git.example.com/",
        "file:///etc/passwd",
    ],
)
def test_normalize_rejects(url):
    with pytest.raises(ValueError):
        normalize_git_url(url)


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://git.example.com/shop/checkout.git", True),
        ("https://git.example.com/shop/nested/repo.git", False),
        ("https://git.example.com/shop-evil/repo.git", False),
        ("https://git.example.com/platform/team/user-mgmt.git", True),
        ("https://git.evil.com/shop/checkout.git", False),
        ("https://user:pw@git.example.com/shop/checkout.git", False),
    ],
)
def test_is_authorized(url, allowed):
    assert is_authorized(SUBJECT, url, CLIENTS) is allowed


def test_unknown_subject_is_denied():
    assert is_authorized("system:serviceaccount:other:sa", "https://git.example.com/shop/checkout.git", CLIENTS) is False
