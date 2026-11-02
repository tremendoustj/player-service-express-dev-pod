import io
import json
import logging
import secrets
import sqlite3
from contextlib import closing
from uuid import UUID

import pytest

import express_api
from express_api import API_PREFIX, MAX_QUERY_STRING_BYTES
from player_service import PlayerQueryTimeoutError, PlayerService


PLAYERS_URL = API_PREFIX + "/players"
DETAIL_URL = PLAYERS_URL + "/aaronha01"
DETAIL_ROUTE = PLAYERS_URL + "/<string:player_id>"


@pytest.fixture(autouse=True)
def verify_connections_closed(tracked_connections):
    yield
    for conn in tracked_connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            sqlite3.Connection.execute(conn, "SELECT 1")


def assert_error(response, status, code):
    assert response.status_code == status
    assert response.is_json
    payload = response.get_json()
    assert set(payload) == {"error", "request_id"}
    assert set(payload["error"]) == {"code", "message"}
    assert payload["error"]["code"] == code
    assert payload["error"]["message"]
    assert payload["request_id"] == response.headers["X-Request-ID"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize(
    "path",
    [API_PREFIX, PLAYERS_URL, DETAIL_URL, PLAYERS_URL + "/bad-id", API_PREFIX + "/unknown"],
)
@pytest.mark.parametrize("method", ["GET", "OPTIONS", "POST", "DELETE"])
def test_authentication_precedes_routing_and_validation(
    express_client, tracked_connections, path, method
):
    response = express_client.open(
        path + "?unknown=secret&limit=invalid", method=method, data=b"not a tool input"
    )
    assert_error(response, 401, "unauthorized")
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="player-service-express"'
    assert tracked_connections == []


@pytest.mark.parametrize(
    "authorization",
    [
        "",
        "Bearer",
        "Bearer short",
        "Basic ignored",
        "Bearer " + "x" * 257,
        "Bearer " + "x" * 42 + "\u00e9",
        "Bearer " + "x" * 43 + " ",
        "Bearer  " + "x" * 43,
        "Bearer\t" + "x" * 43,
        "Bearer " + "x" * 43 + ", Bearer " + "x" * 43,
    ],
)
def test_malformed_authorization_is_a_safe_401(
    express_client, tracked_connections, authorization
):
    response = express_client.get(
        PLAYERS_URL, headers={"Authorization": authorization}
    )
    assert_error(response, 401, "unauthorized")
    assert tracked_connections == []


def test_wrong_well_formed_token_uses_constant_time_comparison(
    express_client, express_token, tracked_connections, monkeypatch
):
    calls = []
    compare_digest = express_api.hmac.compare_digest

    def record_comparison(actual, expected):
        calls.append((actual, expected))
        return compare_digest(actual, expected)

    monkeypatch.setattr(express_api.hmac, "compare_digest", record_comparison)
    wrong = secrets.token_urlsafe(32)
    response = express_client.get(
        PLAYERS_URL, headers={"Authorization": f"Bearer {wrong}"}
    )
    assert_error(response, 401, "unauthorized")
    assert calls == [(wrong, express_token)]
    assert tracked_connections == []


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "bEaReR"])
def test_bearer_scheme_is_case_insensitive(express_client, express_token, scheme):
    response = express_client.get(
        PLAYERS_URL, headers={"Authorization": f"{scheme} {express_token}"}
    )
    assert response.status_code == 200


def test_duplicate_authorization_headers_are_rejected(
    express_client, express_token, tracked_connections
):
    response = express_client.get(
        PLAYERS_URL,
        headers=[
            ("Authorization", f"Bearer {express_token}"),
            ("Authorization", f"Bearer {express_token}"),
        ],
    )
    assert_error(response, 401, "unauthorized")
    assert tracked_connections == []


def test_query_credentials_cannot_authenticate(
    express_client, express_token, tracked_connections
):
    response = express_client.get(
        PLAYERS_URL, query_string={"token": express_token}
    )
    assert_error(response, 401, "unauthorized")
    assert express_token not in response.get_data(as_text=True)
    assert tracked_connections == []


def test_search_response_preserves_records_and_pagination(
    express_client, express_headers, player_records
):
    response = express_client.get(PLAYERS_URL, headers=express_headers)
    assert response.status_code == 200
    assert response.is_json
    payload = response.get_json()
    assert set(payload) == {"data", "pagination", "request_id"}
    assert payload["data"] == player_records
    assert payload["pagination"] == {
        "limit": 20, "offset": 0, "total": 4, "next_offset": None
    }
    assert str(UUID(payload["request_id"])) == response.headers["X-Request-ID"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("record_index", [0, 1, 3])
def test_detail_response_preserves_record_shape_and_nulls(
    express_client, express_headers, player_records, record_index
):
    expected = player_records[record_index]
    response = express_client.get(
        PLAYERS_URL + "/" + expected["playerId"],
        headers={**express_headers, "X-Request-ID": "detail-123"},
    )

    assert response.status_code == 200
    assert response.is_json
    assert response.get_json() == {"data": expected, "request_id": "detail-123"}
    assert response.headers["X-Request-ID"] == "detail-123"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("player_id", ["a", "A", "9", "Ab09" * 8])
def test_detail_accepts_exact_id_boundaries(
    express_client, express_headers, player_db, player_records, player_id
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("INSERT INTO players (playerId) VALUES (?)", (player_id,))
        conn.commit()
    expected = dict.fromkeys(player_records[0])
    expected["playerId"] = player_id

    response = express_client.get(
        PLAYERS_URL + "/" + player_id, headers=express_headers
    )

    assert response.status_code == 200
    assert response.get_json()["data"] == expected


def test_detail_accepts_percent_encoded_ascii_id(
    express_client, express_headers, player_records
):
    response = express_client.get(
        PLAYERS_URL + "/%61aronha01", headers=express_headers
    )

    assert response.status_code == 200
    assert response.get_json()["data"] == player_records[0]


@pytest.mark.parametrize(
    "segment",
    [
        "a" * 33,
        "%20",
        "aaronha01%20",
        "aaronha01%0A",
        "a%00b",
        "a%09b",
        "a_b",
        "a-b",
        "quote%2701",
        "%C3%A9",
        "%EF%BC%A1",
        "%D9%A1",
        "%",
        "%GG",
        "%FF",
        "%C0%AF",
        "%2561aronha01",
        "aaronha01%27%20OR%201%3D1%20--",
    ],
)
def test_invalid_detail_ids_fail_before_any_database_access(
    express_client, express_headers, tracked_connections, segment
):
    response = express_client.get(
        PLAYERS_URL + "/" + segment, headers=express_headers
    )

    assert_error(response, 400, "invalid_request")
    assert tracked_connections == []


@pytest.mark.parametrize(
    "query", ["q=Aaron", "limit=1", "unknown=secret", "a=1&a=2", "q", "&", "%", "q=%FF"]
)
def test_detail_rejects_all_nonempty_query_strings(
    express_client, express_headers, tracked_connections, query
):
    response = express_client.get(DETAIL_URL + "?" + query, headers=express_headers)

    assert_error(response, 400, "invalid_request")
    assert tracked_connections == []


@pytest.mark.parametrize("player_id", ["missing", "Aaronha01", "aaron"])
def test_missing_detail_is_a_redacted_correlated_404(
    express_client, express_headers, player_id, caplog
):
    response = express_client.get(
        PLAYERS_URL + "/" + player_id,
        headers={**express_headers, "X-Request-ID": "missing-detail-1"},
    )

    assert_error(response, 404, "player_not_found")
    event = json.loads(caplog.records[-1].getMessage())
    assert event["operation"] == "express_integration.get_player"
    assert event["route"] == DETAIL_ROUTE
    assert event["status"] == 404
    assert event["request_id"] == "missing-detail-1"
    assert event["error_code"] == "player_not_found"
    assert event["exception_type"] == "PlayersNotFoundError"
    assert event["result_count"] == 0
    assert caplog.records[-1].exc_info is None
    assert player_id not in response.get_json()["error"]["message"]
    assert PLAYERS_URL + "/" + player_id not in caplog.text


def test_duplicate_stored_id_is_a_storage_error_not_an_arbitrary_detail(
    express_client, express_headers, player_db, caplog
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            "INSERT INTO players SELECT * FROM players WHERE playerId = ?", ("aaronha01",)
        )
        conn.commit()

    response = express_client.get(DETAIL_URL, headers=express_headers)

    assert_error(response, 503, "storage_unavailable")
    event = json.loads(caplog.records[-1].getMessage())
    assert event["exception_type"] == "DataError"
    assert event["result_count"] == 0
    assert "aaronha01" not in caplog.text
    assert "aaronha01" not in response.get_data(as_text=True)


@pytest.mark.parametrize(
    ("query", "indexes"),
    [
        ({"q": "Aaron"}, [0, 1]),
        ({"q": "Ren\u00e9"}, [2]),
        ({"q": "O'Neil"}, [2]),
        ({"q": "%"}, []),
        ({"q": "_"}, []),
        ({"q": "' OR 1=1 --"}, []),
        ({"birth_country": "C\u00f4te d'Ivoire"}, [2]),
        ({"birth_year_min": "1939"}, [1]),
        ({"birth_year_max": "1934"}, [0]),
        ({"bats": "B"}, [2]),
        ({"throws": "S"}, [2]),
        ({"birth_country": "missing"}, []),
    ],
)
def test_filters_reuse_shared_semantics(
    express_client, express_headers, player_records, query, indexes
):
    response = express_client.get(
        PLAYERS_URL, query_string=query, headers=express_headers
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data"] == [player_records[index] for index in indexes]
    assert payload["pagination"]["total"] == len(indexes)


def test_all_filters_and_pagination_can_be_combined(
    express_client, express_headers, player_records
):
    response = express_client.get(
        PLAYERS_URL,
        headers=express_headers,
        query_string={
            "q": "Aaron",
            "birth_country": "USA",
            "birth_year_min": "1934",
            "birth_year_max": "1939",
            "bats": "R",
            "throws": "R",
            "limit": "1",
            "offset": "1",
        },
    )
    assert response.status_code == 200
    assert response.get_json()["data"] == [player_records[1]]
    assert response.get_json()["pagination"] == {
        "limit": 1, "offset": 1, "total": 2, "next_offset": None
    }


@pytest.mark.parametrize(
    ("query", "expected_limit", "expected_offset", "expected_total"),
    [
        ({"limit": "1"}, 1, 0, 4),
        ({"limit": "100", "offset": "100000"}, 100, 100000, 4),
        ({"birth_year_min": "1", "birth_year_max": "9999"}, 20, 0, 2),
    ],
)
def test_integer_boundaries(
    express_client, express_headers, query, expected_limit, expected_offset, expected_total
):
    response = express_client.get(
        PLAYERS_URL, query_string=query, headers=express_headers
    )
    assert response.status_code == 200
    page = response.get_json()["pagination"]
    assert page["limit"] == expected_limit
    assert page["offset"] == expected_offset
    assert page["total"] == expected_total


@pytest.mark.parametrize(
    "query",
    [
        "unknown=secret",
        "q=Aaron&q=Hank",
        "limit=1&%6cimit=2",
        "q",
        "q=Aaron&",
        "&q=Aaron",
        "q=%",
        "q=%GG",
        "q=%FF",
        "q=%C0%AF",
        "q=%00",
        "q=",
        "q=%20",
        "birth_country=",
        "birth_country=%0A",
        "q=" + "a" * 101,
        "birth_country=" + "a" * 101,
        "limit=0",
        "limit=101",
        "offset=100001",
        "limit=-1",
        "limit=%2B1",
        "limit=%201",
        "limit=1.0",
        "limit=1e1",
        "limit=01",
        "offset=00",
        "offset=1000000",
        "limit=%EF%BC%91",
        "limit=true",
        "limit=",
        "birth_year_min=0",
        "birth_year_max=10000",
        "birth_year_min=1939&birth_year_max=1934",
        "bats=S",
        "bats=r",
        "throws=B",
        "throws=",
        "&".join("q=a" for _ in range(9)),
    ],
)
def test_invalid_inputs_fail_before_any_database_read(
    express_client, express_headers, tracked_connections, query
):
    response = express_client.get(
        PLAYERS_URL + "?" + query, headers=express_headers
    )
    assert_error(response, 400, "invalid_request")
    assert tracked_connections == []


@pytest.mark.parametrize(
    ("size", "status", "code"),
    [
        (MAX_QUERY_STRING_BYTES, 400, "invalid_request"),
        (MAX_QUERY_STRING_BYTES + 1, 414, "request_too_large"),
    ],
)
@pytest.mark.parametrize("url", [PLAYERS_URL, DETAIL_URL])
def test_exact_query_string_size_boundary(
    express_client, express_headers, tracked_connections, size, status, code, url
):
    response = express_client.get(
        url + "?q=" + "x" * (size - 2), headers=express_headers
    )
    assert_error(response, status, code)
    assert tracked_connections == []


def test_request_size_is_checked_after_authentication(
    express_client, tracked_connections
):
    response = express_client.get(
        PLAYERS_URL + "?q=" + "x" * (MAX_QUERY_STRING_BYTES + 1)
    )
    assert_error(response, 401, "unauthorized")
    assert tracked_connections == []


@pytest.mark.parametrize("input_terminated", [False, True])
@pytest.mark.parametrize("url", [PLAYERS_URL, DETAIL_URL])
def test_request_bodies_are_rejected_without_reading_them(
    express_client, express_headers, tracked_connections, input_terminated, url
):
    class UnreadableBody(io.BytesIO):
        def read(self, *args, **kwargs):
            pytest.fail("A GET body must not be read")

        def readinto(self, *args, **kwargs):
            pytest.fail("A GET body must not be read")

    response = express_client.get(
        url,
        headers=express_headers,
        environ_overrides={
            "CONTENT_LENGTH": "1",
            "wsgi.input": UnreadableBody(b"x"),
            "wsgi.input_terminated": input_terminated,
        },
    )
    assert_error(response, 400, "invalid_request")
    assert tracked_connections == []


@pytest.mark.parametrize("input_terminated", [False, True])
@pytest.mark.parametrize("url", [PLAYERS_URL, DETAIL_URL])
def test_transfer_encoded_bodies_are_rejected(
    express_client, express_headers, tracked_connections, input_terminated, url
):
    response = express_client.get(
        url,
        headers=express_headers,
        environ_overrides={
            "HTTP_TRANSFER_ENCODING": "chunked",
            "wsgi.input_terminated": input_terminated,
        },
    )
    assert_error(response, 400, "invalid_request")
    assert tracked_connections == []


@pytest.mark.parametrize("content_length", [None, "0"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_server_safe_stream_marker_does_not_imply_a_body(
    express_client, express_headers, player_records, content_length, method
):
    overrides = {"wsgi.input_terminated": True}
    if content_length is not None:
        overrides["CONTENT_LENGTH"] = content_length
    response = express_client.open(
        PLAYERS_URL, method=method, headers=express_headers, environ_overrides=overrides
    )
    assert response.status_code == 200
    if method == "GET":
        assert response.get_json()["data"] == player_records
    else:
        assert response.data == b""


@pytest.mark.parametrize("request_id", ["a", "client-123.test_4", "x" * 64])
@pytest.mark.parametrize("authorized", [True, False])
def test_safe_request_ids_round_trip_on_success_and_failure(
    express_client, express_headers, request_id, authorized
):
    headers = dict(express_headers) if authorized else {}
    headers["X-Request-ID"] = request_id
    response = express_client.get(PLAYERS_URL, headers=headers)
    assert response.status_code == (200 if authorized else 401)
    assert response.headers["X-Request-ID"] == request_id
    assert response.get_json()["request_id"] == request_id


@pytest.mark.parametrize("request_id", ["", "x" * 65, "a b", "a/b", "a,b", "\u00e9", ".a"])
def test_unsafe_request_ids_are_replaced(
    express_client, express_headers, request_id
):
    response = express_client.get(
        PLAYERS_URL, headers={**express_headers, "X-Request-ID": request_id}
    )
    assert response.status_code == 200
    generated = response.headers["X-Request-ID"]
    assert generated != request_id
    assert str(UUID(generated)) == generated
    assert response.get_json()["request_id"] == generated


@pytest.mark.parametrize(
    ("path", "method", "status", "code"),
    [
        (API_PREFIX, "GET", 404, "not_found"),
        (API_PREFIX + "/unknown", "GET", 404, "not_found"),
        (DETAIL_URL + "/extra", "GET", 404, "not_found"),
        (DETAIL_URL + "/", "GET", 404, "not_found"),
        (PLAYERS_URL + "/a%2Fb", "GET", 404, "not_found"),
        (PLAYERS_URL, "POST", 405, "method_not_allowed"),
        (PLAYERS_URL, "DELETE", 405, "method_not_allowed"),
        (DETAIL_URL, "POST", 405, "method_not_allowed"),
        (DETAIL_URL, "DELETE", 405, "method_not_allowed"),
    ],
)
def test_routing_errors_are_correlated_and_do_not_query(
    express_client, express_headers, tracked_connections, path, method, status, code
):
    response = express_client.open(path, method=method, headers=express_headers)
    assert_error(response, status, code)
    if status == 405:
        assert set(response.headers["Allow"].split(", ")) == {"GET", "HEAD", "OPTIONS"}
    assert tracked_connections == []


@pytest.mark.parametrize("url", [PLAYERS_URL, DETAIL_URL])
def test_head_and_options_require_authentication(
    express_client, express_headers, tracked_connections, url
):
    assert express_client.head(url).status_code == 401
    assert express_client.options(url).status_code == 401
    assert tracked_connections == []

    options = express_client.options(url, headers=express_headers)
    assert options.status_code == 200
    assert "X-Request-ID" in options.headers
    assert tracked_connections == []
    head = express_client.head(url, headers=express_headers)
    assert head.status_code == 200
    assert head.data == b""
    assert "X-Request-ID" in head.headers


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, {"busy_timeout_seconds": 1.0, "query_timeout_seconds": 2.0}),
        (
            {"PLAYER_DB_BUSY_TIMEOUT_SECONDS": "0", "PLAYER_QUERY_TIMEOUT_SECONDS": "3"},
            {"busy_timeout_seconds": 0.0, "query_timeout_seconds": 3.0},
        ),
    ],
)
@pytest.mark.parametrize("url", [PLAYERS_URL, DETAIL_URL])
def test_http_reads_use_configured_deadlines(
    express_app, express_headers, monkeypatch, overrides, expected, url
):
    import app

    calls = []

    def bounded_service(**kwargs):
        calls.append(kwargs)
        return PlayerService(**kwargs)

    monkeypatch.setattr(express_api, "PlayerService", bounded_service)
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    with app.create_app().test_client() as client:
        response = client.get(url, headers=express_headers)
    assert response.status_code == 200
    assert calls == [expected]


@pytest.mark.parametrize("stage", ["constructor", "read"])
@pytest.mark.parametrize(
    ("path", "operation", "query", "route"),
    [
        (PLAYERS_URL, "search_players", {"q": "sensitive-search"}, PLAYERS_URL),
        (PLAYERS_URL + "/sensitiveplayer", "get_player", None, DETAIL_ROUTE),
    ],
)
@pytest.mark.parametrize(
    ("exception", "status", "code"),
    [
        (sqlite3.OperationalError("private SQL and filesystem detail"), 503, "storage_unavailable"),
        (PlayerQueryTimeoutError(), 504, "query_timeout"),
        (ValueError("private application diagnostic"), 500, "internal_error"),
    ],
)
def test_safe_error_mapping_and_redacted_failure_logs(
    express_client, express_headers, express_token, monkeypatch, caplog,
    stage, exception, status, code, path, operation, query, route
):
    def fail(*args, **kwargs):
        raise exception

    if stage == "constructor":
        monkeypatch.setattr(express_api, "PlayerService", fail)
    else:
        monkeypatch.setattr(PlayerService, operation, fail)
    response = express_client.get(
        path,
        headers={**express_headers, "X-Request-ID": "failure-123"},
        query_string=query,
    )
    assert_error(response, status, code)
    event = json.loads(caplog.records[-1].getMessage())
    assert event["status"] == status
    assert event["request_id"] == "failure-123"
    assert event["error_code"] == code
    assert event["exception_type"] == type(exception).__name__
    assert event["result_count"] == 0
    assert event["route"] == route
    assert caplog.records[-1].exc_info is None
    for sensitive in (
        express_token, "sensitive-search", "sensitiveplayer", "private", "Authorization"
    ):
        assert sensitive not in caplog.text
        assert sensitive not in response.get_data(as_text=True)


@pytest.mark.parametrize("url", [PLAYERS_URL, DETAIL_URL])
def test_missing_storage_returns_503_without_recreating_it(
    express_client, express_headers, player_db, url
):
    player_db.unlink()
    response = express_client.get(url, headers=express_headers)
    assert_error(response, 503, "storage_unavailable")
    assert not player_db.exists()


@pytest.mark.parametrize("invalid", [float("inf"), -float("inf"), b"not-json"])
@pytest.mark.parametrize("url", [PLAYERS_URL, DETAIL_URL])
def test_invalid_stored_json_never_becomes_a_successful_response(
    express_client, express_headers, player_db, invalid, url
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("UPDATE players SET height = ? WHERE playerId = ?", (invalid, "aaronha01"))
        conn.commit()
    response = express_client.get(url, headers=express_headers)
    assert_error(response, 503, "storage_unavailable")
    assert "Infinity" not in response.get_data(as_text=True)
    assert "not-json" not in response.get_data(as_text=True)


@pytest.mark.parametrize("url", [PLAYERS_URL, DETAIL_URL])
def test_real_lock_exhaustion_is_503_and_releases_connections(
    express_app, express_headers, player_db, monkeypatch, url
):
    import app

    monkeypatch.setenv("PLAYER_DB_BUSY_TIMEOUT_SECONDS", "0.02")
    monkeypatch.setenv("PLAYER_QUERY_TIMEOUT_SECONDS", "0.1")
    with app.create_app().test_client() as client:
        with closing(sqlite3.connect(player_db)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            response = client.get(url, headers=express_headers)
            assert_error(response, 503, "storage_unavailable")
            writer.rollback()
        assert client.get(url, headers=express_headers).status_code == 200


@pytest.mark.parametrize("url", [PLAYERS_URL, DETAIL_URL])
def test_real_query_cancellation_is_504_and_subsequent_reads_succeed(
    express_app, express_headers, monkeypatch, url
):
    import app

    connect = sqlite3.connect

    class ExpensiveReadConnection(sqlite3.Connection):
        def execute(self, query, parameters=()):
            if query in (
                "SELECT COUNT(*) FROM players",
                "SELECT * FROM players WHERE playerId COLLATE BINARY = ? LIMIT ?",
            ):
                query = (
                    "WITH RECURSIVE n(i) AS (VALUES(0) UNION ALL "
                    "SELECT i + 1 FROM n WHERE i < 10000000) SELECT SUM(i) FROM n"
                )
                parameters = ()
            return super().execute(query, parameters)

    def expensive_connect(*args, **kwargs):
        return connect(*args, factory=ExpensiveReadConnection, **kwargs)

    monkeypatch.setenv("PLAYER_QUERY_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setattr(sqlite3, "connect", expensive_connect)
    with app.create_app().test_client() as client:
        response = client.get(url, headers=express_headers)
        assert_error(response, 504, "query_timeout")
        monkeypatch.setattr(sqlite3, "connect", connect)
        assert client.get(url, headers=express_headers).status_code == 200


@pytest.mark.parametrize(
    ("path", "query", "operation", "route"),
    [
        (PLAYERS_URL, {"q": "Hank"}, "search_players", PLAYERS_URL),
        (DETAIL_URL, None, "get_player", DETAIL_ROUTE),
    ],
)
def test_success_logs_are_structured_without_player_or_query_data(
    express_client, express_headers, express_token, caplog, path, query, operation, route
):
    caplog.set_level(logging.INFO)
    response = express_client.get(
        path, query_string=query, headers=express_headers
    )
    assert response.status_code == 200
    event = json.loads(caplog.records[-1].getMessage())
    assert event["event"] == "express_request"
    assert event["operation"] == "express_integration." + operation
    assert event["route"] == route
    assert event["status"] == 200
    assert event["result_count"] == 1
    assert event["error_code"] is None
    assert event["exception_type"] is None
    assert event["duration_ms"] >= 0
    assert event["timestamp"].endswith("+00:00")
    assert event["request_id"] == response.get_json()["request_id"]
    for sensitive in ("Hank", "aaronha01", express_token, "Authorization"):
        assert sensitive not in caplog.text


def test_unmatched_routes_and_auth_failures_do_not_log_raw_inputs(
    express_client, express_token, caplog
):
    response = express_client.get(
        API_PREFIX + "/sensitive-path",
        query_string={"token": express_token},
        headers={"X-Request-ID": "unsafe request identifier"},
    )
    assert_error(response, 401, "unauthorized")
    event = json.loads(caplog.records[-1].getMessage())
    assert event["operation"] == "unmatched"
    assert event["route"] == "unmatched"
    assert event["error_code"] == "unauthorized"
    for sensitive in ("sensitive-path", "unsafe request identifier", express_token):
        assert sensitive not in caplog.text


def test_complete_seed_pagination_through_authenticated_http(
    express_client, express_headers, seed_player_records
):
    expected_ids = sorted(record["playerId"] for record in seed_player_records)
    assert len(expected_ids) == 19370
    received_ids = []
    offset = 0
    while offset is not None:
        response = express_client.get(
            PLAYERS_URL,
            query_string={"limit": "100", "offset": str(offset)},
            headers=express_headers,
        )
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["pagination"]["total"] == 19370
        assert payload["pagination"]["offset"] == offset
        assert len(payload["data"]) <= 100
        received_ids.extend(record["playerId"] for record in payload["data"])
        next_offset = payload["pagination"]["next_offset"]
        assert next_offset is None or next_offset == offset + 100
        offset = next_offset
    assert received_ids == expected_ids
    assert len(set(received_ids)) == 19370


def test_seed_details_match_independent_csv_records(
    express_client, express_headers, seed_player_records
):
    assert len(seed_player_records) == 19370
    selected = [
        seed_player_records[0],
        seed_player_records[len(seed_player_records) // 2],
        seed_player_records[-1],
    ]
    for field in ("birthYear", "height", "weight"):
        selected.append(next(row for row in seed_player_records if row[field] is None))
    numeric_columns = (
        "birthYear", "birthMonth", "birthDay", "deathYear", "deathMonth", "deathDay",
        "height", "weight",
    )
    for row in selected:
        expected = {
            column: (
                float(value) if column in numeric_columns and value is not None else value
            )
            for column, value in row.items()
        }
        response = express_client.get(
            PLAYERS_URL + "/" + row["playerId"], headers=express_headers
        )
        assert response.status_code == 200
        assert response.get_json()["data"] == expected


def test_legacy_player_routes_stay_unauthenticated_and_unchanged(
    express_client, player_records
):
    listing = express_client.get("/v1/players")
    detail = express_client.get("/v1/players/aaronha01")
    quoted_detail = express_client.get("/v1/players/quote%2701")
    absent = express_client.get("/v1/players/missing")
    assert listing.get_json() == player_records
    assert detail.get_json() == player_records[0]
    assert quoted_detail.get_json() == player_records[2]
    assert absent.status_code == 200
    assert absent.get_json() == {"error": "No record found with player_id=missing"}
    for response in (listing, detail, quoted_detail, absent):
        assert "X-Request-ID" not in response.headers
        assert "Cache-Control" not in response.headers


@pytest.mark.parametrize("path", ["/unknown", "/integrations/express/v10/players"])
def test_other_routing_errors_keep_legacy_http_behavior(express_client, path):
    response = express_client.get(path)
    assert response.status_code == 404
    assert not response.is_json
    assert "X-Request-ID" not in response.headers


def test_legacy_exceptions_are_not_swallowed(express_client, monkeypatch):
    def fail(self):
        raise RuntimeError("legacy failure")

    monkeypatch.setattr(PlayerService, "get_all_players", fail)
    with pytest.raises(RuntimeError, match="legacy failure"):
        express_client.get("/v1/players")


@pytest.mark.parametrize("unavailable", [False, True])
def test_legacy_ollama_routes_are_not_integration_tools(
    express_client, monkeypatch, unavailable
):
    import app

    def list_models():
        if unavailable:
            raise ConnectionError("mock offline")
        return {"models": [{"name": "tinyllama"}]}

    def chat(**kwargs):
        assert kwargs == {
            "model": "tinyllama",
            "messages": [{"role": "user", "content": "Why is the sky blue?"}],
        }
        if unavailable:
            raise ConnectionError("mock offline")
        return {"message": {"role": "assistant", "content": "mock answer"}}

    monkeypatch.setattr(app.ollama, "list", list_models)
    monkeypatch.setattr(app.ollama, "chat", chat)
    listing = express_client.get("/v1/chat/list-models")
    conversation = express_client.post("/v1/chat")
    assert listing.status_code == conversation.status_code == (503 if unavailable else 200)
    if unavailable:
        assert listing.get_json()["detail"] == "mock offline"
        assert conversation.get_json()["detail"] == "mock offline"
    else:
        assert listing.get_json() == {"models": [{"name": "tinyllama"}]}
        assert conversation.get_json() == {
            "message": {"role": "assistant", "content": "mock answer"}
        }
    assert "X-Request-ID" not in listing.headers
    assert "X-Request-ID" not in conversation.headers
