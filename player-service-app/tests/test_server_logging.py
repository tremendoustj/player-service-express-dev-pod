import json
import logging
import sys

import pytest

from express_api import API_PREFIX
from server_logging import ProductionJsonFormatter


@pytest.mark.parametrize("level", [logging.INFO, logging.WARNING, logging.ERROR])
def test_unstructured_logs_keep_metadata_without_rendering_private_content(level):
    class PrivateArgument:
        def __str__(self):
            pytest.fail("Free-form log arguments must not be rendered")

    record = logging.LogRecord(
        "gunicorn.error", level, "/private/path/server.py", 42,
        "private-message %s", (PrivateArgument(),), None, "handle_error",
        "private-stack",
    )
    record.private_extra = "private-extra"

    output = ProductionJsonFormatter().format(record)
    event = json.loads(output)

    assert event == {
        "event": "server_log",
        "timestamp": event["timestamp"],
        "level": logging.getLevelName(level),
        "logger": "gunicorn.error",
        "process_id": record.process,
        "source": "server.handle_error:42",
        "exception_type": None,
    }
    assert event["timestamp"].endswith("+00:00")
    assert "private" not in output
    assert "\n" not in output


def test_exception_logs_retain_class_but_not_message_traceback_or_stack():
    try:
        raise ValueError("private-exception")
    except ValueError:
        record = logging.LogRecord(
            "app", logging.ERROR, "/private/path/app.py", 10,
            "private-message", (), sys.exc_info(), "create_app", "private-stack",
        )
    record.exc_text = "private-cached-traceback"

    output = ProductionJsonFormatter().format(record)

    assert json.loads(output)["exception_type"] == "ValueError"
    assert "private" not in output
    assert "Traceback" not in output


def test_json_shaped_unstructured_messages_cannot_become_request_events():
    record = logging.LogRecord(
        "gunicorn.error", logging.WARNING, "server.py", 1,
        '{"event":"express_request","private":"payload"}', (), None,
    )

    output = ProductionJsonFormatter().format(record)

    assert json.loads(output)["event"] == "server_log"
    assert "private" not in output
    assert "payload" not in output


def test_exception_logging_without_an_active_exception_is_still_redacted():
    record = logging.LogRecord(
        "gunicorn.error", logging.ERROR, "server.py", 1,
        "private-message", (), (None, None, None),
    )

    output = ProductionJsonFormatter().format(record)

    assert json.loads(output)["exception_type"] is None
    assert "private" not in output


@pytest.mark.parametrize("authorized", [True, False])
def test_real_request_events_remain_structured_and_redacted(
    express_client, express_headers, express_token, caplog, authorized
):
    caplog.set_level(logging.INFO)
    response = express_client.get(
        API_PREFIX + "/players?q=private-query",
        headers={
            **(express_headers if authorized else {}),
            "X-Request-ID": "structured-request",
            "User-Agent": "private-user-agent",
        },
    )
    record = caplog.records[-1]
    original = json.loads(record.getMessage())

    output = ProductionJsonFormatter().format(record)
    event = json.loads(output)

    assert record.express_event == original
    assert event == {
        **original,
        "level": "INFO" if authorized else "WARNING",
        "logger": "app",
        "process_id": record.process,
    }
    assert event["status"] == response.status_code == (200 if authorized else 401)
    assert event["request_id"] == "structured-request"
    assert event["route"] == API_PREFIX + "/players"
    assert "\n" not in output
    for private in (express_token, "private-query", "private-user-agent"):
        assert private not in output
