import httpx
import pytest

from pacds.config import LogsConfig
from pacds.errors import PacdsError
from pacds.request import LogSource
from pacds.workspace.logs import LogFetcher

URL = "https://bucket.s3.amazonaws.com/server.log?X-Amz-Signature=secret"
CONFIG = LogsConfig(allowed_hosts=["*.s3.amazonaws.com"], max_file_size_mb=1)


def public_resolver(address="52.216.1.1"):
    async def resolve(host, port):
        return [address]

    return resolve


def fetcher(handler, config=CONFIG, resolve=None) -> LogFetcher:
    return LogFetcher(config, transport=httpx.MockTransport(handler), resolve=resolve or public_resolver())


async def fetch(log_fetcher, tmp_path, url=URL):
    await log_fetcher.fetch_all([LogSource(name="server.log", url=url)], tmp_path)


async def expect_error(log_fetcher, tmp_path, url=URL) -> PacdsError:
    with pytest.raises(PacdsError) as error:
        await fetch(log_fetcher, tmp_path, url)
    assert "secret" not in error.value.message
    return error.value


async def test_downloads_log(tmp_path):
    await fetch(fetcher(lambda request: httpx.Response(200, content=b"line1\nline2\n")), tmp_path)
    assert (tmp_path / "server.log").read_bytes() == b"line1\nline2\n"


async def test_host_not_allowed(tmp_path):
    error = await expect_error(fetcher(lambda r: httpx.Response(200)), tmp_path, "https://evil.example.com/x.log")
    assert (error.status, error.code) == (422, "log_host_not_allowed")


async def test_http_rejected_unless_allowed(tmp_path):
    url = "http://bucket.s3.amazonaws.com/server.log"
    error = await expect_error(fetcher(lambda r: httpx.Response(200, content=b"ok")), tmp_path, url)
    assert error.status == 422
    allow_http = LogsConfig(allowed_hosts=["*.s3.amazonaws.com"], allow_http=True)
    await fetch(fetcher(lambda r: httpx.Response(200, content=b"ok"), config=allow_http), tmp_path, url)


@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1", "::ffff:10.0.0.1", "100.64.0.1"])
async def test_non_public_addresses_rejected(tmp_path, address):
    error = await expect_error(fetcher(lambda r: httpx.Response(200), resolve=public_resolver(address)), tmp_path)
    assert (error.status, error.code) == (422, "log_host_not_allowed")


async def test_private_addresses_allowed_when_configured(tmp_path):
    config = LogsConfig(allowed_hosts=["*.s3.amazonaws.com"], allow_private_ips=True)
    await fetch(fetcher(lambda r: httpx.Response(200, content=b"ok"), config=config, resolve=public_resolver("10.0.0.5")), tmp_path)


async def test_redirect_is_not_followed(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})

    error = await expect_error(fetcher(handler), tmp_path)
    assert (error.status, error.code) == (422, "log_redirect_refused")
    assert len(calls) == 1


async def test_storage_4xx_is_422_and_5xx_is_502(tmp_path):
    assert (await expect_error(fetcher(lambda r: httpx.Response(403)), tmp_path)).status == 422
    assert (await expect_error(fetcher(lambda r: httpx.Response(503)), tmp_path)).status == 502


async def test_too_large_is_rejected_and_removed(tmp_path):
    body = b"x" * (1024 * 1024 + 1)
    error = await expect_error(fetcher(lambda r: httpx.Response(200, content=body)), tmp_path)
    assert (error.status, error.code) == (422, "log_too_large")
    assert not (tmp_path / "server.log").exists()


async def test_timeout_is_502(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert (await expect_error(fetcher(handler), tmp_path)).status == 502


async def test_private_addresses_allowed_only_for_listed_hosts(tmp_path):
    config = LogsConfig(allowed_hosts=["*.s3.amazonaws.com", "minio.corp.example"], private_hosts=["minio.corp.example"])
    private = public_resolver("10.0.0.5")
    await fetch(fetcher(lambda r: httpx.Response(200, content=b"ok"), config=config, resolve=private), tmp_path,
                "https://minio.corp.example/logs/app.log?X-Amz-Signature=s")
    error = await expect_error(fetcher(lambda r: httpx.Response(200), config=config, resolve=private), tmp_path)
    assert error.code == "log_host_not_allowed"
