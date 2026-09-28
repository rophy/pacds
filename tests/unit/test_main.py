import logging

import httpx
import pytest

from pacds.main import configure_logging


@pytest.mark.parametrize("logger", ["httpx", "httpx2"])
def test_request_logs_drop_query_strings(logger, caplog):
    configure_logging()
    url = httpx.URL("http://s3.test:9000/logs/app.log?X-Amz-Credential=ak&X-Amz-Signature=secret")
    with caplog.at_level(logging.INFO, logger=logger):
        logging.getLogger(logger).info('HTTP Request: %s %s "%s %d %s"', "GET", url, "HTTP/1.1", 200, "OK")
    assert "secret" not in caplog.text and "X-Amz" not in caplog.text
    assert 'GET http://s3.test:9000/logs/app.log "HTTP/1.1 200 OK"' in caplog.text


def test_configure_logging_is_idempotent(caplog):
    configure_logging()
    configure_logging()
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info("HTTP Request: %s %s", "GET", "http://h/p?sig=secret")
    assert caplog.text.count("http://h/p") == 1 and "secret" not in caplog.text


def test_httpx2_url_objects_are_redacted(caplog):
    import httpx2

    configure_logging()
    with caplog.at_level(logging.INFO, logger="httpx2"):
        logging.getLogger("httpx2").info("HTTP Request: %s %s", "POST", httpx2.URL("https://llm.test/v1/responses?key=secret"))
    assert "secret" not in caplog.text and "https://llm.test/v1/responses" in caplog.text


def test_log_lines_carry_the_request_id():
    from pacds.context import request_id
    from pacds.main import LOG_FORMAT, _RequestId

    record = logging.LogRecord("pacds.engine", logging.WARNING, __file__, 1, "engine failed: %s", ("boom",), None)
    token = request_id.set("abc123")
    try:
        assert _RequestId().filter(record)
    finally:
        request_id.reset(token)
    assert logging.Formatter(LOG_FORMAT).format(record) == "WARNING:pacds.engine:request=abc123:engine failed: boom"
    outside = logging.LogRecord("x", logging.INFO, __file__, 1, "startup", (), None)
    _RequestId().filter(outside)
    assert outside.request_id == "-"
