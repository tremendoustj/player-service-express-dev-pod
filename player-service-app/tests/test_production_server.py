import http.client
import json
import os
import runpy
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from types import SimpleNamespace

import pytest

if sys.platform == "win32" or sys.version_info < (3, 10):
    pytest.skip(
        "The optional Gunicorn production profile requires Unix and Python 3.10+",
        allow_module_level=True,
    )

from gunicorn.config import Config
from prometheus_client.parser import text_string_to_metric_families

from app_paths import APP_DIR
from express_api import API_PREFIX


def production_config():
    namespace = runpy.run_path(str(APP_DIR / "gunicorn.conf.py"))
    config = Config()
    for name, value in namespace.items():
        if name in config.settings:
            config.set(name, value)
    return config


def test_production_defaults_preserve_the_single_process_logging_and_metrics_model():
    config = production_config()

    assert config.wsgi_app == "wsgi:app"
    assert config.bind == ["127.0.0.1:8000"]
    assert config.workers == 1
    assert config.worker_class_str == "gthread"
    assert config.threads == 4
    assert config.preload_app is False
    assert config.reload is False
    assert config.control_socket_disable is True
    assert config.timeout == config.graceful_timeout == 30
    assert config.keepalive == 2
    assert config.limit_request_line == 8190
    assert config.limit_request_fields == 32
    assert config.limit_request_field_size == 4096
    assert config.forwarded_allow_ips == []
    assert config.secure_scheme_headers == {}
    assert config.accesslog is None
    assert config.capture_output is False
    assert config.syslog is False
    config.on_starting(SimpleNamespace(cfg=config))
    config.nworkers_changed(None, 1, None)


@pytest.mark.parametrize("workers", [0, 2, 4])
def test_worker_count_changes_fail_closed(workers):
    config = production_config()

    with pytest.raises(RuntimeError, match="exactly one worker"):
        config.nworkers_changed(None, workers, 1)


@pytest.mark.parametrize(
    "overrides",
    [
        {"workers": 2},
        {"worker_class": "sync", "threads": 1},
        {"reload": True},
    ],
)
def test_unsupported_production_overrides_fail_closed(overrides):
    config = production_config()
    for name, value in overrides.items():
        config.set(name, value)

    with pytest.raises(RuntimeError, match="Player Service|production profile"):
        config.on_starting(SimpleNamespace(cfg=config))


@pytest.fixture
def server_directory(tmp_path):
    directory = tmp_path / "application"
    directory.mkdir()
    for filename in (
        "app.py", "app_paths.py", "express_api.py", "express_metrics.py",
        "player_service.py", "server_logging.py", "gunicorn.conf.py", "wsgi.py",
        "Player.csv",
    ):
        shutil.copy2(APP_DIR / filename, directory / filename)
    return directory


@pytest.fixture
def server_environment(express_token):
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("PLAYER_", "GUNICORN_", "FLASK_", "PROMETHEUS_"))
        and name not in ("prometheus_multiproc_dir", "WEB_CONCURRENCY", "PORT", "PYTHONPATH")
    }
    environment.update(
        PLAYER_INTEGRATION_ENABLED="true",
        PLAYER_INTEGRATION_TOKEN=express_token,
        PYTHONUNBUFFERED="1",
    )
    return environment


@contextmanager
def running_server(directory, environment, arguments=()):
    stdout_path = directory / "stdout.log"
    stderr_path = directory / "stderr.log"
    with ExitStack() as stack:
        listener = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        stdout = stack.enter_context(stdout_path.open("w"))
        stderr = stack.enter_context(stderr_path.open("w"))
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        process = subprocess.Popen(
            [
                sys.executable, "-m", "gunicorn", "--config", "gunicorn.conf.py",
                "--bind", f"fd://{listener.fileno()}", "--graceful-timeout", "2",
                *arguments,
            ],
            cwd=directory,
            env=environment,
            pass_fds=(listener.fileno(),),
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        listener.close()
        try:
            yield process, port
        finally:
            # The isolated process group contains only this test's server/workers.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                os.killpg(process.pid, signal.SIGKILL)
                pytest.fail("A server worker remained after the master's shutdown")


def request(port, path, *, headers=None, method="GET", body=None, timeout=5):
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)) as conn:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.headers, response.read()


def wait_until_ready(process, port):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        assert process.poll() is None, "Production server exited before readiness"
        try:
            status, _, _ = request(port, "/__local_readiness_probe__", timeout=0.2)
        except (OSError, http.client.RemoteDisconnected):
            time.sleep(0.02)
        else:
            assert status == 404
            return
    pytest.fail("Production server did not become responsive on loopback")


def read_events(directory, expected_stderr=""):
    assert (directory / "stderr.log").read_text() == expected_stderr
    text = (directory / "stdout.log").read_text()
    assert text
    return text, [json.loads(line) for line in text.splitlines()]


def test_fresh_server_exercises_all_tools_metrics_errors_and_redacted_wire_logs(
    server_directory, server_environment, express_token, seed_player_records,
    expected_statistics, expected_comparisons,
):
    expected_events = {}
    database = server_directory / "player.db"
    assert not database.exists()
    records = {record["playerId"]: record for record in seed_player_records}
    comparison = expected_comparisons(
        [records["aaronha01"], records["aaronto01"]]
    )
    private_values = (
        express_token, "private-search", "private-country", "private-user-agent",
        "private-unknown-path", "private-body", "private-header", "private-version",
        "aaronha01", "aaronto01",
    )

    with running_server(server_directory, server_environment) as (process, port):
        wait_until_ready(process, port)
        assert database.is_file()
        initial_database = database.read_bytes()

        def call(
            path, request_id, expected_status=200, *, authorized=True,
            method="GET", body=None,
        ):
            status, headers, data = request(
                port, API_PREFIX + path, method=method, body=body,
                headers={
                    **({"Authorization": f"Bearer {express_token}"} if authorized else {}),
                    "X-Request-ID": request_id,
                    "User-Agent": "private-user-agent",
                },
            )
            assert status == expected_status
            assert headers["X-Request-ID"] == request_id
            assert headers["Cache-Control"] == "no-store"
            assert headers["X-Content-Type-Options"] == "nosniff"
            expected_events[request_id] = status
            if data and headers.get_content_type() == "application/json":
                result = json.loads(data)
                assert result["request_id"] == request_id
                return result
            return data

        page = call("/players?q=Aaron&limit=1", "wire-search")
        assert len(page["data"]) == 1
        assert page["pagination"]["next_offset"] == 1
        following = call("/players?q=Aaron&limit=1&offset=1", "wire-next-page")
        assert following["data"][0]["playerId"] != page["data"][0]["playerId"]
        assert following["pagination"]["total"] == page["pagination"]["total"]
        filtered = call("/players?birth_country=USA&bats=R&limit=1", "wire-filter")
        assert filtered["pagination"]["total"] == sum(
            record["birthCountry"] == "USA" and record["bats"] == "R"
            for record in seed_player_records
        )
        detail = call("/players/aaronha01", "wire-detail")
        assert detail["data"] == comparison["data"][0]
        statistics = call("/statistics", "wire-statistics")
        assert statistics == {
            **expected_statistics(seed_player_records), "request_id": "wire-statistics",
        }
        ranking = call("/rankings?metric=height&limit=2", "wire-rankings")
        known_heights = [r for r in seed_player_records if r["height"] is not None]
        ordered = sorted(known_heights, key=lambda r: (-float(r["height"]), r["playerId"]))
        assert [r["playerId"] for r in ranking["data"]] == [
            r["playerId"] for r in ordered[:2]
        ]
        assert ranking["pagination"]["total"] == len(known_heights)
        assert ranking["excluded_count"] == len(seed_player_records) - len(known_heights)
        assert call(
            "/comparisons?player_ids=aaronha01,aaronto01", "wire-comparisons",
        ) == {**comparison, "request_id": "wire-comparisons"}
        assert call("/players?q=private-search", "wire-empty")["data"] == []
        assert call("/players", "wire-head", method="HEAD") == b""
        assert call("/players", "wire-options", method="OPTIONS") == b""
        for path, request_id, status, code in (
            ("/players?q=x&q=y", "wire-invalid", 400, "invalid_request"),
            ("/players/missing01", "wire-missing", 404, "player_not_found"),
            ("/private-unknown-path", "wire-unmatched", 404, "not_found"),
            ("/players?q=" + "x" * 4097, "wire-too-large", 414, "request_too_large"),
        ):
            assert call(path, request_id, status)["error"]["code"] == code
        assert call(
            "/players?birth_country=private-country", "wire-unauthorized", 401,
            authorized=False,
        )["error"]["code"] == "unauthorized"
        assert call(
            "/players", "wire-method", 405, method="POST", body="private-body",
        )["error"]["code"] == "method_not_allowed"
        assert call(
            "/players", "wire-body", 400, body="private-body",
        )["error"]["code"] == "invalid_request"

        def concurrent_read(index):
            request_id = f"wire-thread-{index}"
            status, headers, data = request(
                port, API_PREFIX + "/players?limit=1",
                headers={
                    "Authorization": f"Bearer {express_token}",
                    "X-Request-ID": request_id,
                },
            )
            assert status == 200
            assert headers["X-Request-ID"] == request_id
            assert json.loads(data)["request_id"] == request_id
            return request_id

        with ThreadPoolExecutor(max_workers=8) as executor:
            for request_id in executor.map(concurrent_read, range(16)):
                expected_events[request_id] = 200

        unavailable = database.with_name("unavailable.db")
        database.rename(unavailable)
        try:
            assert call(
                "/players", "wire-storage-error", 503,
            )["error"]["code"] == "storage_unavailable"
            metrics = call("/metrics", "wire-metrics").decode()
        finally:
            unavailable.rename(database)
        samples = [
            sample
            for family in text_string_to_metric_families(metrics)
            for sample in family.samples
        ]
        counts = {
            (sample.labels["operation"], sample.labels["status"]): sample.value
            for sample in samples
            if sample.name == "player_service_express_requests_total"
        }
        assert counts[("express_integration.search_players", "200")] == 22
        assert counts[("express_integration.search_players", "401")] == 1
        assert counts[("express_integration.search_players", "503")] == 1
        assert counts[("express_integration.get_player", "200")] == 1
        assert counts[("express_integration.get_player_statistics", "200")] == 1
        assert counts[("express_integration.rank_players", "200")] == 1
        assert counts[("express_integration.compare_players", "200")] == 1

        for raw_request in (
            b"GET / HTTP/private-version\r\nHost: localhost\r\n\r\n",
            b"GET / HTTP/1.1\r\nHost: localhost\r\nprivate-header bad: value\r\n\r\n",
        ):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
                conn.sendall(raw_request)
                assert conn.recv(4096).startswith(b"HTTP/1.1 400")

        status, _, body = request(port, "/v1/players/aaronha01")
        assert status == 200
        assert json.loads(body) == comparison["data"][0]
        assert database.read_bytes() == initial_database

    text, events = read_events(server_directory)
    requests = [event for event in events if event["event"] == "express_request"]
    assert Counter(event["request_id"] for event in requests) == Counter(
        expected_events.keys()
    )
    assert {event["request_id"]: event["status"] for event in requests} == expected_events
    assert len({event["process_id"] for event in requests}) == 1
    assert all(event["process_id"] != process.pid for event in requests)
    assert all(
        event["level"] == ("WARNING" if event["status"] >= 400 else "INFO")
        for event in requests
    )
    assert sum(
        event["event"] == "server_log" and event["level"] == "WARNING"
        for event in events
    ) >= 2
    assert all(event["logger"] != "gunicorn.access" for event in events)
    for private in private_values:
        assert private not in text
        assert private not in metrics


@pytest.mark.parametrize("enabled", [True, False])
def test_existing_read_only_database_preserves_legacy_and_opt_in_behavior(
    server_directory, server_environment, player_db, player_records, enabled,
):
    database = server_directory / "player.db"
    shutil.copy2(player_db, database)
    database.chmod(0o444)
    initial_database = database.read_bytes()
    if not enabled:
        server_environment.pop("PLAYER_INTEGRATION_ENABLED")
        server_environment.pop("PLAYER_INTEGRATION_TOKEN")
    server_environment["WEB_CONCURRENCY"] = "8"
    server_environment["PORT"] = "9999"

    with running_server(server_directory, server_environment) as (process, port):
        wait_until_ready(process, port)
        status, headers, body = request(
            port, API_PREFIX + "/players",
            headers=(
                {"Authorization": f"Bearer {server_environment['PLAYER_INTEGRATION_TOKEN']}"}
                if enabled else {}
            ),
        )
        assert status == (200 if enabled else 404)
        assert ("X-Request-ID" in headers) == enabled
        if enabled:
            assert json.loads(body)["data"] == player_records
        status, _, body = request(port, "/v1/players/aaronha01")
        assert status == 200
        assert json.loads(body) == player_records[0]
        assert database.read_bytes() == initial_database

    _, events = read_events(server_directory)
    assert sum(event["event"] == "express_request" for event in events) == int(enabled)


@pytest.mark.parametrize(
    "overrides",
    [
        {"PLAYER_INTEGRATION_TOKEN": "private-invalid-token"},
        {"PLAYER_INTEGRATION_ENABLED": "private-invalid-flag"},
        {"PLAYER_QUERY_TIMEOUT_SECONDS": "private-invalid-timeout"},
        {"PROMETHEUS_MULTIPROC_DIR": "private-multiprocess-path"},
    ],
)
def test_invalid_app_configuration_exits_without_initializing_storage_or_leaking_values(
    server_directory, server_environment, overrides,
):
    server_environment.update(overrides)
    with running_server(server_directory, server_environment) as (process, _):
        assert process.wait(timeout=15) != 0

    assert not (server_directory / "player.db").exists()
    text, events = read_events(
        server_directory,
        expected_stderr="Player Service startup failed; inspect structured logs\n",
    )
    assert any(event.get("exception_type") == "ValueError" for event in events)
    assert any(event["level"] == "ERROR" for event in events)
    for value in overrides.values():
        assert value not in text


@pytest.mark.parametrize(
    "arguments",
    [("--workers", "2"), ("--worker-class", "sync", "--threads", "1"), ("--reload",)],
)
def test_unsupported_server_configuration_exits_before_app_initialization(
    server_directory, server_environment, arguments,
):
    with running_server(server_directory, server_environment, arguments) as (process, _):
        assert process.wait(timeout=15) != 0

    assert not (server_directory / "player.db").exists()
    text = (
        (server_directory / "stdout.log").read_text()
        + (server_directory / "stderr.log").read_text()
    )
    assert "Error: " in text
    assert server_environment["PLAYER_INTEGRATION_TOKEN"] not in text


@pytest.mark.parametrize("failure", ["missing_csv", "malformed_csv", "unwritable_db"])
def test_storage_initialization_failures_are_fatal_without_raw_boot_diagnostics(
    server_directory, server_environment, failure,
):
    csv_path = server_directory / "Player.csv"
    if failure == "missing_csv":
        csv_path.unlink()
        exception_type = "FileNotFoundError"
    elif failure == "malformed_csv":
        csv_path.write_text('private-column\n"unterminated-private-value', encoding="utf-8")
        exception_type = "ParserError"
    else:
        (server_directory / "player.db").symlink_to(
            server_directory / "private-missing-directory" / "player.db"
        )
        exception_type = "OperationalError"

    with running_server(server_directory, server_environment) as (process, _):
        assert process.wait(timeout=15) != 0

    assert not (server_directory / "player.db").exists()
    text, events = read_events(
        server_directory,
        expected_stderr="Player Service startup failed; inspect structured logs\n",
    )
    assert any(event.get("exception_type") == exception_type for event in events)
    assert "private" not in text
    assert str(server_directory) not in text
    assert server_environment["PLAYER_INTEGRATION_TOKEN"] not in text


def test_http_parser_enforces_exact_request_line_and_header_boundaries(
    server_directory, server_environment, player_db, express_token,
):
    shutil.copy2(player_db, server_directory / "player.db")
    base_line = f"GET {API_PREFIX}/players?q="
    protocol = " HTTP/1.1"
    base_headers = ["Host: localhost", f"Authorization: Bearer {express_token}"]
    expected_ids = set()

    with running_server(server_directory, server_environment) as (process, port):
        wait_until_ready(process, port)
        for kind in ("request_line", "header_count", "header_size"):
            for over_limit in (False, True):
                request_id = f"boundary-{kind}-{int(over_limit)}"
                headers = [*base_headers, f"X-Request-ID: {request_id}"]
                line = f"GET {API_PREFIX}/players?limit=1 HTTP/1.1"
                if kind == "request_line":
                    size = 8190 + int(over_limit)
                    line = base_line + "x" * (size - len(base_line + protocol)) + protocol
                    assert len(line.encode("ascii")) == size
                    expected_status = 400 if over_limit else 414
                elif kind == "header_count":
                    headers.extend(
                        f"X-Pad-{index}: private-header-value"
                        for index in range(32 + int(over_limit) - len(headers))
                    )
                    assert len(headers) == 32 + int(over_limit)
                    expected_status = 431 if over_limit else 200
                else:
                    field = "X-Pad: "
                    field += "p" * (4096 + int(over_limit) - len(field) - 2)
                    assert len(field.encode("ascii")) + 2 == 4096 + int(over_limit)
                    headers.append(field)
                    expected_status = 431 if over_limit else 200
                wire = ("\r\n".join([line, *headers]) + "\r\n\r\n").encode("ascii")
                with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
                    conn.sendall(wire)
                    with http.client.HTTPResponse(conn) as response:
                        response.begin()
                        assert response.status == expected_status
                        assert ("X-Request-ID" in response.headers) == (not over_limit)
                        response.read()
                if not over_limit:
                    expected_ids.add(request_id)

    text, events = read_events(server_directory)
    assert {
        event["request_id"] for event in events if event["event"] == "express_request"
    } == expected_ids
    assert express_token not in text
    assert "private-header-value" not in text
