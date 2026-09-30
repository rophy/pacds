"""Evaluation outside the dev stack: tokens from any OIDC issuer, any S3-compatible log store."""

from urllib.parse import parse_qs, urlsplit

import json
import httpx
import pytest

from pacds_eval.oidc import REFRESH_MARGIN_SECONDS, TokenSource
from pacds_eval.s3 import LogStore


def issuer(tokens):
    seen = []

    def handler(request):
        seen.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": tokens.pop(0), "expires_in": 300, "token_type": "Bearer"})

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


ENV = {"PACDS_OIDC_TOKEN_URL": "https://sso.corp.example/token", "PACDS_OIDC_CLIENT_ID": "eval", "PACDS_OIDC_CLIENT_SECRET": "s",
       "PACDS_OIDC_SCOPE": "pacds", "PACDS_OIDC_AUDIENCE": "pacds"}


def test_a_static_token_wins():
    assert TokenSource({"PACDS_TOKEN": "t", **ENV}).get() == "t"


def test_client_credentials_tokens_are_reused_then_refreshed_before_expiry():
    now = [0.0]
    http, seen = issuer(["a", "b"])
    source = TokenSource(ENV, clock=lambda: now[0], http=http)
    assert source.kind == "client_credentials" and source.get() == "a" and source.get() == "a"
    assert seen[0]["grant_type"] == ["client_credentials"] and seen[0]["audience"] == ["pacds"] and seen[0]["scope"] == ["pacds"]
    now[0] = 300 - REFRESH_MARGIN_SECONDS
    assert source.get() == "b" and len(seen) == 2


def test_a_refused_token_request_says_why():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="invalid_client")))
    with pytest.raises(RuntimeError, match="invalid_client"):
        TokenSource(ENV, http=http).get()


def test_without_configuration_the_dev_mock_is_used():
    assert TokenSource({}).kind == "dev-mock"


def test_log_store_signs_for_the_host_pacds_fetches_from():
    store = LogStore.from_env({"PACDS_LOGS_S3_ENDPOINT": "https://minio.corp.example", "PACDS_LOGS_S3_BUCKET": "triage",
                               "PACDS_LOGS_S3_ACCESS_KEY": "k", "PACDS_LOGS_S3_SECRET_KEY": "s",
                               "PACDS_LOGS_S3_UPLOAD_ENDPOINT": "http://localhost:9000"})
    url = urlsplit(store.presign("replay/c1/app.log"))
    assert (url.scheme, url.hostname, url.path) == ("https", "minio.corp.example", "/triage/replay/c1/app.log")
    assert "X-Amz-Signature" in url.query and store.host == "minio.corp.example"


def test_defaults_are_the_dev_stack():
    store = LogStore.from_env({})
    assert store.endpoint == "http://s3:9000" and store.bucket == "logs" and store.access_key == "pacds-dev"


def test_seed_uploads_the_logs_each_case_lists(tmp_path, monkeypatch):
    for case, logs in (("c1", ["a.log", "b.txt"]), ("c2", [])):
        (tmp_path / case).mkdir()
        (tmp_path / case / "case.json").write_text(json.dumps({"logs": logs}))
        (tmp_path / case / "unlisted.log").write_text("x")
        for name in logs:
            (tmp_path / case / name).write_text("x")
    uploads = []
    store = LogStore(upload_endpoint="http://localhost:9000")
    monkeypatch.setattr(LogStore, "_client", lambda self, endpoint: type("C", (), {"upload_file": lambda _, f, b, k: uploads.append((endpoint, b, k))})())
    assert store.seed([tmp_path]) == 2
    assert uploads == [("http://localhost:9000", "logs", "replay/c1/a.log"), ("http://localhost:9000", "logs", "replay/c1/b.txt")]
    (tmp_path / "c1" / "b.txt").unlink()
    with pytest.raises(FileNotFoundError, match="b.txt"):
        store.seed([tmp_path])
