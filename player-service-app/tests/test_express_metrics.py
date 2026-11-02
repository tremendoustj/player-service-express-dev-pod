import json
import logging
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY
from prometheus_client.parser import text_string_to_metric_families

import express_api
import express_metrics
from express_api import API_PREFIX, MAX_QUERY_STRING_BYTES
from express_metrics import ExpressMetrics
from player_service import PlayerQueryTimeoutError, PlayerService


METRICS_URL = API_PREFIX + "/metrics"
PLAYERS_URL = API_PREFIX + "/players"
METRIC_PREFIX = "player_service_express_"
SCRAPE_OPERATION = "express_integration.get_metrics"
SEARCH_OPERATION = "express_integration.search_players"
READ_OPERATIONS = (
    ("search_players", PLAYERS_URL),
    ("get_player", PLAYERS_URL + "/aaronha01"),
    ("get_player_statistics", API_PREFIX + "/statistics"),
    ("rank_players", API_PREFIX + "/rankings?metric=height"),
    ("compare_players", API_PREFIX + "/comparisons?player_ids=aaronha01,aaronto01"),
)


@pytest.fixture
def metrics(express_app):
    return express_app.extensions["express_metrics"]


def metric_value(metrics, suffix, **labels):
    return metrics.registry.get_sample_value(METRIC_PREFIX + suffix, labels)


def samples(text):
    return [
        sample
        for family in text_string_to_metric_families(text)
        for sample in family.samples
    ]


def test_new_registry_contains_only_empty_integration_metric_families():
    metrics = ExpressMetrics()
    families = list(text_string_to_metric_families(metrics.render().decode()))
    assert {family.name: family.type for family in families} == {
        METRIC_PREFIX + "requests": "counter",
        METRIC_PREFIX + "errors": "counter",
        METRIC_PREFIX + "request_duration_seconds": "histogram",
    }
    assert all(not family.samples for family in families)
    assert not any(
        family.name.startswith(METRIC_PREFIX) for family in REGISTRY.collect()
    )


def test_histogram_uses_seconds_and_exact_cumulative_bucket_boundaries():
    metrics = ExpressMetrics()
    durations = [0, 0.005, 0.006, 0.02, 0.1, 2, 2.1, 120, 121]
    for duration in durations:
        metrics.observe(
            operation=SEARCH_OPERATION, status=200,
            duration_seconds=duration, error_code=None,
        )
    buckets = {
        float(sample.labels["le"]): sample.value
        for sample in samples(metrics.render().decode())
        if sample.name == METRIC_PREFIX + "request_duration_seconds_bucket"
    }
    bounds = [
        0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120,
        float("inf"),
    ]
    assert buckets == {
        bound: sum(duration <= bound for duration in durations) for bound in bounds
    }
    assert metric_value(
        metrics, "request_duration_seconds_count", operation=SEARCH_OPERATION
    ) == len(durations)
    assert metric_value(
        metrics, "request_duration_seconds_sum", operation=SEARCH_OPERATION
    ) == pytest.approx(sum(durations))
    assert metric_value(
        metrics, "requests_total", operation=SEARCH_OPERATION, status="200"
    ) == len(durations)


@pytest.mark.parametrize(("status", "label"), [(99, "other"), (100, "100"), (599, "599"), (600, "other")])
def test_status_labels_are_bounded(status, label):
    metrics = ExpressMetrics()
    metrics.observe(
        operation=SEARCH_OPERATION, status=status,
        duration_seconds=0.1, error_code=None,
    )
    assert metric_value(
        metrics, "requests_total", operation=SEARCH_OPERATION, status=label
    ) == 1


def test_unrecognized_operations_and_codes_cannot_grow_label_cardinality():
    metrics = ExpressMetrics()
    for index in range(1000):
        metrics.observe(
            operation=f"private-operation-{index}", status=600 + index,
            duration_seconds=0.125, error_code=f"private-error-{index}",
        )
    text = metrics.render().decode()
    assert "private" not in text
    assert metric_value(
        metrics, "requests_total", operation="unmatched", status="other"
    ) == 1000
    assert metric_value(
        metrics, "errors_total", operation="unmatched", code="http_error"
    ) == 1000
    assert metric_value(
        metrics, "request_duration_seconds_count", operation="unmatched"
    ) == 1000
    for sample in samples(text):
        assert sample.labels["operation"] == "unmatched"
        assert set(sample.labels) <= {"operation", "status", "code", "le"}


def test_concurrent_observations_do_not_lose_counts_or_latency():
    metrics = ExpressMetrics()

    def observe_batch(_):
        for index in range(100):
            metrics.observe(
                operation=SEARCH_OPERATION,
                status=401 if index % 2 else 200,
                duration_seconds=0.125,
                error_code="unauthorized" if index % 2 else None,
            )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(observe_batch, range(8)))
    for status in ("200", "401"):
        assert metric_value(
            metrics, "requests_total", operation=SEARCH_OPERATION, status=status
        ) == 400
    assert metric_value(
        metrics, "errors_total", operation=SEARCH_OPERATION, code="unauthorized"
    ) == 400
    assert metric_value(
        metrics, "request_duration_seconds_count", operation=SEARCH_OPERATION
    ) == 800
    assert metric_value(
        metrics, "request_duration_seconds_sum", operation=SEARCH_OPERATION
    ) == 100


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
@pytest.mark.parametrize("authorization", ["missing", "malformed", "wrong"])
def test_scrape_authentication_precedes_validation_and_database_access(
    express_client, metrics, tracked_connections, method, authorization
):
    headers = {}
    if authorization == "malformed":
        headers["Authorization"] = "Basic private-credential"
    elif authorization == "wrong":
        headers["Authorization"] = "Bearer " + secrets.token_urlsafe(32)
    response = express_client.open(
        METRICS_URL + "?unsupported=private-input", method=method, headers=headers
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer ")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Request-ID"]
    assert metric_value(
        metrics, "requests_total", operation=SCRAPE_OPERATION, status="401"
    ) == 1
    assert metric_value(
        metrics, "errors_total", operation=SCRAPE_OPERATION, code="unauthorized"
    ) == 1
    assert tracked_connections == []


def test_scrape_exports_prometheus_text_with_headers_and_counts_itself_after_render(
    express_client, express_headers, express_token, metrics, tracked_connections
):
    response = express_client.get(
        METRICS_URL,
        headers={**express_headers, "X-Request-ID": "private-scrape-id"},
    )
    assert response.status_code == 200
    assert response.content_type == CONTENT_TYPE_LATEST
    assert response.headers["X-Request-ID"] == "private-scrape-id"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    text = response.get_data(as_text=True)
    assert samples(text) == []
    assert "private-scrape-id" not in text
    assert express_token not in text
    assert metric_value(
        metrics, "requests_total", operation=SCRAPE_OPERATION, status="200"
    ) == 1

    response = express_client.get(METRICS_URL, headers=express_headers)
    counter = next(
        sample for sample in samples(response.get_data(as_text=True))
        if sample.name == METRIC_PREFIX + "requests_total"
    )
    assert counter.labels == {"operation": SCRAPE_OPERATION, "status": "200"}
    assert counter.value == 1
    assert metric_value(
        metrics, "requests_total", operation=SCRAPE_OPERATION, status="200"
    ) == 2
    assert metric_value(
        metrics, "request_duration_seconds_count", operation=SCRAPE_OPERATION
    ) == 2
    assert tracked_connections == []


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_scrape_head_and_options_are_authenticated_bounded_and_counted(
    express_client, express_headers, metrics, tracked_connections, method
):
    response = express_client.open(METRICS_URL, method=method, headers=express_headers)
    assert response.status_code == 200
    assert response.data == b""
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Request-ID"]
    if method == "HEAD":
        assert response.content_type == CONTENT_TYPE_LATEST
        assert response.content_length > 0
    else:
        assert set(response.headers["Allow"].split(", ")) == {"GET", "HEAD", "OPTIONS"}
    assert metric_value(
        metrics, "requests_total", operation=SCRAPE_OPERATION, status="200"
    ) == 1
    assert tracked_connections == []


@pytest.mark.parametrize(
    ("query", "status", "code"),
    [
        ("?unknown=private", 400, "invalid_request"),
        ("?name[]=requests_total", 400, "invalid_request"),
        ("?broken", 400, "invalid_request"),
        ("?q=%FF", 400, "invalid_request"),
        ("?" + "x" * (MAX_QUERY_STRING_BYTES + 1), 414, "request_too_large"),
    ],
)
def test_scrape_rejects_all_query_inputs(
    express_client, express_headers, metrics, tracked_connections, query, status, code
):
    response = express_client.get(METRICS_URL + query, headers=express_headers)
    assert response.status_code == status
    assert response.get_json()["error"]["code"] == code
    assert metric_value(
        metrics, "errors_total", operation=SCRAPE_OPERATION, code=code
    ) == 1
    assert tracked_connections == []


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("body_kind", ["declared", "chunked"])
def test_scrape_rejects_bodies_without_reading_them(
    express_client, express_headers, metrics, tracked_connections, method, body_kind
):
    headers = dict(express_headers)
    if body_kind == "chunked":
        headers["Transfer-Encoding"] = "chunked"
    response = express_client.open(
        METRICS_URL, method=method, headers=headers,
        data=b"private-body" if body_kind == "declared" else None,
    )
    assert response.status_code == 400
    assert metric_value(
        metrics, "errors_total", operation=SCRAPE_OPERATION, code="invalid_request"
    ) == 1
    assert tracked_connections == []


@pytest.mark.parametrize("method", ["POST", "DELETE", "PRIVATE"])
def test_scrape_rejects_unsupported_methods_and_preserves_allow_header(
    express_client, express_headers, metrics, tracked_connections, method
):
    response = express_client.open(METRICS_URL, method=method, headers=express_headers)
    assert response.status_code == 405
    assert set(response.headers["Allow"].split(", ")) == {"GET", "HEAD", "OPTIONS"}
    assert response.get_json()["error"]["code"] == "method_not_allowed"
    assert metric_value(
        metrics, "errors_total", operation="unmatched", code="method_not_allowed"
    ) == 1
    assert tracked_connections == []


@pytest.mark.parametrize(("operation", "path"), READ_OPERATIONS)
def test_all_reads_record_one_request_and_share_the_log_latency_measurement(
    express_client, express_headers, metrics, monkeypatch, caplog, operation, path
):
    caplog.set_level(logging.INFO)
    times = iter((100.0, 100.125))
    monkeypatch.setattr(express_api, "monotonic", lambda: next(times))
    response = express_client.get(path, headers=express_headers)
    assert response.status_code == 200
    operation = "express_integration." + operation
    assert metric_value(
        metrics, "requests_total", operation=operation, status="200"
    ) == 1
    assert metric_value(
        metrics, "request_duration_seconds_count", operation=operation
    ) == 1
    assert metric_value(
        metrics, "request_duration_seconds_sum", operation=operation
    ) == 0.125
    assert json.loads(caplog.records[-1].getMessage())["duration_ms"] == 125
    assert not any(
        sample.name == METRIC_PREFIX + "errors_total"
        for sample in samples(metrics.render().decode())
    )


@pytest.mark.parametrize(("operation", "path"), READ_OPERATIONS)
@pytest.mark.parametrize(
    ("exception", "status", "code"),
    [
        (sqlite3.OperationalError("private storage detail"), 503, "storage_unavailable"),
        (PlayerQueryTimeoutError(), 504, "query_timeout"),
        (RuntimeError("private exception message"), 500, "internal_error"),
    ],
)
def test_all_read_failures_are_counted_once_with_safe_codes_and_latency(
    express_client, express_headers, metrics, monkeypatch,
    operation, path, exception, status, code
):
    def fail(*args, **kwargs):
        raise exception

    monkeypatch.setattr(PlayerService, operation, fail)
    response = express_client.get(path, headers=express_headers)
    assert response.status_code == status
    assert response.get_json()["error"]["code"] == code
    operation = "express_integration." + operation
    assert metric_value(
        metrics, "requests_total", operation=operation, status=str(status)
    ) == 1
    assert metric_value(
        metrics, "errors_total", operation=operation, code=code
    ) == 1
    assert metric_value(
        metrics, "request_duration_seconds_count", operation=operation
    ) == 1
    assert metric_value(
        metrics, "request_duration_seconds_sum", operation=operation
    ) >= 0
    assert "private" not in metrics.render().decode()


@pytest.mark.parametrize(
    ("path", "operation", "status", "code"),
    [
        (PLAYERS_URL + "?limit=0", "search_players", 400, "invalid_request"),
        (PLAYERS_URL + "/bad-id", "get_player", 400, "invalid_request"),
        (API_PREFIX + "/statistics?limit=1", "get_player_statistics", 400, "invalid_request"),
        (API_PREFIX + "/rankings", "rank_players", 400, "invalid_request"),
        (API_PREFIX + "/comparisons?player_ids=one", "compare_players", 400, "invalid_request"),
        (PLAYERS_URL + "/missing01", "get_player", 404, "player_not_found"),
        (
            API_PREFIX + "/comparisons?player_ids=aaronha01,missing01",
            "compare_players", 404, "player_not_found",
        ),
        (API_PREFIX + "/private-unknown", "unmatched", 404, "not_found"),
    ],
)
def test_validation_missing_player_and_routing_errors_are_observed(
    express_client, express_headers, metrics, path, operation, status, code
):
    response = express_client.get(path, headers=express_headers)
    assert response.status_code == status
    if operation != "unmatched":
        operation = "express_integration." + operation
    assert metric_value(
        metrics, "requests_total", operation=operation, status=str(status)
    ) == 1
    assert metric_value(
        metrics, "errors_total", operation=operation, code=code
    ) == 1
    assert metric_value(
        metrics, "request_duration_seconds_count", operation=operation
    ) == 1


def test_scrapes_still_work_when_player_storage_is_missing(
    express_client, express_headers, player_db, tracked_connections
):
    player_db.unlink()
    response = express_client.get(METRICS_URL, headers=express_headers)
    assert response.status_code == 200
    assert response.content_type == CONTENT_TYPE_LATEST
    assert not player_db.exists()
    assert tracked_connections == []


def test_metrics_render_failure_is_sanitized_logged_and_counted(
    express_client, express_headers, metrics, monkeypatch, caplog, tracked_connections
):
    def fail(registry):
        raise RuntimeError("private metrics failure")

    monkeypatch.setattr(express_metrics, "generate_latest", fail)
    response = express_client.get(METRICS_URL, headers=express_headers)
    assert response.status_code == 500
    assert response.get_json()["error"]["code"] == "internal_error"
    assert "private" not in response.get_data(as_text=True)
    assert "private" not in caplog.text
    event = json.loads(caplog.records[-1].getMessage())
    assert event["operation"] == SCRAPE_OPERATION
    assert event["exception_type"] == "RuntimeError"
    assert event["result_count"] == 0
    assert metric_value(
        metrics, "errors_total", operation=SCRAPE_OPERATION, code="internal_error"
    ) == 1
    assert tracked_connections == []


def test_http_labels_never_contain_ids_queries_paths_credentials_or_methods(
    express_client, express_headers, express_token, metrics
):
    for index in range(10):
        headers = {**express_headers, "X-Request-ID": f"private-request-{index}"}
        assert express_client.get(
            PLAYERS_URL, query_string={"q": f"private-query-{index}"}, headers=headers
        ).status_code == 200
        assert express_client.get(
            PLAYERS_URL + f"/privateplayer{index}", headers=headers
        ).status_code == 404
        assert express_client.get(
            API_PREFIX + f"/private-path-{index}",
            query_string={"token": express_token},
        ).status_code == 401
        assert express_client.open(
            PLAYERS_URL, method=f"PRIVATE{index}", headers=headers
        ).status_code == 405
    response = express_client.get(METRICS_URL, headers=express_headers)
    text = response.get_data(as_text=True)
    assert "private" not in text
    assert "PRIVATE" not in text
    assert express_token not in text
    assert "Authorization" not in text
    assert API_PREFIX not in text
    exported_samples = samples(text)
    assert {sample.labels["operation"] for sample in exported_samples} == {
        SEARCH_OPERATION, "express_integration.get_player", "unmatched",
    }
    for sample in exported_samples:
        assert set(sample.labels) <= {"operation", "status", "code", "le"}
    for operation, status in (
        (SEARCH_OPERATION, "200"), ("express_integration.get_player", "404"),
        ("unmatched", "401"), ("unmatched", "405"),
    ):
        assert metric_value(
            metrics, "requests_total", operation=operation, status=status
        ) == 10


def test_legacy_and_out_of_scope_requests_are_not_instrumented(
    express_client, metrics, player_records
):
    before = metrics.render()
    response = express_client.get("/v1/players")
    assert response.status_code == 200
    assert response.get_json() == player_records
    assert "X-Request-ID" not in response.headers
    for path in ("/metrics", "/integrations/express/v10/metrics", "/unknown"):
        response = express_client.get(path)
        assert response.status_code == 404
        assert not response.is_json
        assert "X-Request-ID" not in response.headers
    assert metrics.render() == before


def test_each_application_gets_independent_counters_without_global_registration(
    express_client, express_headers, metrics
):
    import app

    express_client.get(PLAYERS_URL, headers=express_headers)
    other_app = app.create_app()
    other_metrics = other_app.extensions["express_metrics"]
    assert other_metrics is not metrics
    assert other_metrics.registry is not metrics.registry
    assert samples(other_metrics.render().decode()) == []
    with other_app.test_client() as other_client:
        other_client.get(PLAYERS_URL, headers=express_headers)
        other_client.get(PLAYERS_URL, headers=express_headers)
    assert metric_value(
        metrics, "requests_total", operation=SEARCH_OPERATION, status="200"
    ) == 1
    assert metric_value(
        other_metrics, "requests_total", operation=SEARCH_OPERATION, status="200"
    ) == 2
    assert not any(
        family.name.startswith(METRIC_PREFIX) for family in REGISTRY.collect()
    )


def test_threaded_flask_requests_keep_counts_and_request_ids_isolated(
    express_app, express_headers, metrics
):
    def make_request(index):
        request_id = f"thread-{index}"
        with express_app.test_client() as client:
            response = client.get(
                PLAYERS_URL + "?limit=1",
                headers={**express_headers, "X-Request-ID": request_id},
            )
        assert response.status_code == 200
        assert response.get_json()["request_id"] == request_id
        assert response.headers["X-Request-ID"] == request_id

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(make_request, range(30)))
    assert metric_value(
        metrics, "requests_total", operation=SEARCH_OPERATION, status="200"
    ) == 30
    assert metric_value(
        metrics, "request_duration_seconds_count", operation=SEARCH_OPERATION
    ) == 30


@pytest.mark.parametrize("name", ["PROMETHEUS_MULTIPROC_DIR", "prometheus_multiproc_dir"])
@pytest.mark.parametrize("value", ["", "/private/metrics-directory"])
def test_unsupported_multiprocess_mode_fails_before_database_initialization(
    client, express_token, monkeypatch, name, value
):
    import app

    def unexpected_initialization():
        pytest.fail("Unsupported metrics configuration must fail before DB initialization")

    monkeypatch.setenv("PLAYER_INTEGRATION_ENABLED", "true")
    monkeypatch.setenv("PLAYER_INTEGRATION_TOKEN", express_token)
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(app, "ensure_player_db", unexpected_initialization)
    with pytest.raises(ValueError, match="single-process") as caught:
        app.create_app()
    assert "/private" not in str(caught.value)


@pytest.mark.parametrize("name", ["PROMETHEUS_MULTIPROC_DIR", "prometheus_multiproc_dir"])
def test_disabled_integration_does_not_initialize_metrics_or_reject_unused_mode(
    client, monkeypatch, name
):
    import app

    monkeypatch.setenv(name, "/private/metrics-directory")
    application = app.create_app()
    assert "express_metrics" not in application.extensions
    with application.test_client() as disabled_client:
        response = disabled_client.get(METRICS_URL)
    assert response.status_code == 404
    assert "X-Request-ID" not in response.headers


def test_real_lock_failure_and_recovery_are_measured(
    express_app, express_headers, player_db, monkeypatch
):
    import app

    monkeypatch.setenv("PLAYER_DB_BUSY_TIMEOUT_SECONDS", "0")
    application = app.create_app()
    metrics = application.extensions["express_metrics"]
    with application.test_client() as client:
        with closing(sqlite3.connect(player_db)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            assert client.get(PLAYERS_URL, headers=express_headers).status_code == 503
            writer.rollback()
        assert client.get(PLAYERS_URL, headers=express_headers).status_code == 200
    for status in ("200", "503"):
        assert metric_value(
            metrics, "requests_total", operation=SEARCH_OPERATION, status=status
        ) == 1
    assert metric_value(
        metrics, "errors_total", operation=SEARCH_OPERATION, code="storage_unavailable"
    ) == 1
    assert metric_value(
        metrics, "request_duration_seconds_count", operation=SEARCH_OPERATION
    ) == 2


def test_real_sqlite_deadline_and_recovery_are_measured(
    express_app, express_headers, monkeypatch, tracked_connections
):
    import app

    connect = sqlite3.connect

    class ExpensiveConnection(sqlite3.Connection):
        def execute(self, query, parameters=()):
            if query == "SELECT COUNT(*) FROM players":
                query = (
                    "WITH RECURSIVE n(i) AS (VALUES(0) UNION ALL "
                    "SELECT i + 1 FROM n WHERE i < 10000000) SELECT SUM(i) FROM n"
                )
            return super().execute(query, parameters)

    def expensive_connect(*args, **kwargs):
        return connect(*args, factory=ExpensiveConnection, **kwargs)

    monkeypatch.setenv("PLAYER_QUERY_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setattr(sqlite3, "connect", expensive_connect)
    application = app.create_app()
    metrics = application.extensions["express_metrics"]
    with application.test_client() as client:
        response = client.get(PLAYERS_URL, headers=express_headers)
        assert response.status_code == 504
        assert response.get_json()["error"]["code"] == "query_timeout"
        monkeypatch.setattr(sqlite3, "connect", connect)
        assert client.get(PLAYERS_URL, headers=express_headers).status_code == 200
    for status in ("200", "504"):
        assert metric_value(
            metrics, "requests_total", operation=SEARCH_OPERATION, status=status
        ) == 1
    assert metric_value(
        metrics, "errors_total", operation=SEARCH_OPERATION, code="query_timeout"
    ) == 1
    assert metric_value(
        metrics, "request_duration_seconds_count", operation=SEARCH_OPERATION
    ) == 2
    for conn in tracked_connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            sqlite3.Connection.execute(conn, "SELECT 1")
