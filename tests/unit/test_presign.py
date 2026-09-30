from urllib.parse import parse_qs, urlsplit

from pacds_eval.s3 import S3_HOST, presign


def test_presigned_url_points_at_the_in_cluster_host_path_style():
    url = urlsplit(presign("replay/case-1/issue.log", access_key="ak", secret_key="sk"))
    assert (url.scheme, url.netloc) == ("http", f"{S3_HOST}:9000")
    assert url.path == "/logs/replay/case-1/issue.log"


def test_presigned_url_is_signed_and_expires():
    query = parse_qs(urlsplit(presign("e2e/checkout.log", access_key="ak", secret_key="sk", expires=600)).query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["600"]
    assert query["X-Amz-Credential"][0].startswith("ak/")
    assert len(query["X-Amz-Signature"][0]) == 64


def test_signature_depends_on_the_secret():
    assert presign("k", access_key="ak", secret_key="one") != presign("k", access_key="ak", secret_key="two")
