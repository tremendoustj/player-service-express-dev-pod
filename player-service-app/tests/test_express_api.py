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
STATISTICS_URL = API_PREFIX + "/statistics"
RANKINGS_URL = API_PREFIX + "/rankings"
HEIGHT_RANKINGS_URL = RANKINGS_URL + "?metric=height"
READ_URLS = [PLAYERS_URL, DETAIL_URL, STATISTICS_URL, HEIGHT_RANKINGS_URL]


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
    [
        API_PREFIX, PLAYERS_URL, DETAIL_URL, STATISTICS_URL, RANKINGS_URL,
        PLAYERS_URL + "/bad-id", API_PREFIX + "/unknown",
    ],
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
    ("query", "indexes"),
    [
        ({}, [0, 1, 2, 3]),
        ({"group_limit": "1"}, [0, 1, 2, 3]),
        ({"q": "AARON"}, [0, 1]),
        ({"q": "Henry Louis"}, [0]),
        ({"q": "ren\u00e9"}, [2]),
        ({"q": "REN\u00c9"}, []),
        ({"q": "unknown01"}, [3]),
        ({"q": "' OR 1=1 --"}, []),
        ({"q": "%"}, []),
        ({"q": "_"}, []),
        ({"q": "\\"}, []),
        ({"birth_country": "C\u00f4te d'Ivoire"}, [2]),
        ({"birth_country": "usa"}, []),
        ({"birth_country": "USA' OR 1=1 --"}, []),
        ({"birth_year_min": "1939"}, [1]),
        ({"birth_year_max": "1934"}, [0]),
        ({"birth_year_min": "1", "birth_year_max": "9999"}, [0, 1]),
        ({"bats": "B"}, [2]),
        ({"throws": "S"}, [2]),
        ({"q": "x" * 100, "birth_country": "x" * 100}, []),
        (
            {
                "q": "Aaron",
                "birth_country": "USA",
                "birth_year_min": "1934",
                "birth_year_max": "1939",
                "bats": "R",
                "throws": "R",
                "group_limit": "1",
            },
            [0, 1],
        ),
    ],
)
def test_statistics_response_preserves_filters_nulls_and_population_counts(
    express_client, express_headers, player_records, expected_statistics,
    caplog, query, indexes
):
    caplog.set_level(logging.INFO)
    response = express_client.get(
        STATISTICS_URL,
        query_string=query,
        headers={**express_headers, "X-Request-ID": "statistics-123"},
    )
    expected = expected_statistics(
        [player_records[index] for index in indexes], int(query.get("group_limit", "20"))
    )

    assert response.status_code == 200
    assert response.is_json
    assert response.get_json() == {**expected, "request_id": "statistics-123"}
    assert response.headers["X-Request-ID"] == "statistics-123"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    event = json.loads(caplog.records[-1].getMessage())
    assert event["operation"] == "express_integration.get_player_statistics"
    assert event["result_count"] == len(indexes)


def test_statistics_accepts_percent_encoded_parameters(
    express_client, express_headers, player_records, expected_statistics
):
    response = express_client.get(
        STATISTICS_URL + "?%67roup_limit=%31&birth_country=C%C3%B4te+d%27Ivoire",
        headers=express_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {
        **expected_statistics([player_records[2]], group_limit=1),
        "request_id": response.headers["X-Request-ID"],
    }


@pytest.mark.parametrize("group_limit", [None, 1, 100])
def test_statistics_group_limit_bounds_output_not_the_population(
    express_client, express_headers, player_db, player_records,
    expected_statistics, group_limit
):
    records = [
        {**dict.fromkeys(player_records[0]), "birthCountry": f"C{index:03d}"}
        for index in reversed(range(101))
    ]
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.executemany(
            "INSERT INTO players (birthCountry) VALUES (?)",
            [(record["birthCountry"],) for record in records],
        )
        conn.commit()
    query = {} if group_limit is None else {"group_limit": str(group_limit)}

    response = express_client.get(
        STATISTICS_URL, query_string=query, headers=express_headers
    )

    assert response.status_code == 200
    limit = 20 if group_limit is None else group_limit
    payload = response.get_json()
    assert payload == {
        **expected_statistics(records, group_limit=limit),
        "request_id": response.headers["X-Request-ID"],
    }
    assert payload["total"] == 101
    countries = payload["groups"]["birthCountry"]
    assert len(countries["data"]) == limit
    assert countries["total_groups"] == 101
    assert countries["other_count"] == 101 - limit


def test_statistics_empty_database_is_a_successful_empty_population(
    express_client, express_headers, player_db, expected_statistics
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.commit()

    response = express_client.get(STATISTICS_URL, headers=express_headers)

    assert response.status_code == 200
    assert response.get_json() == {
        **expected_statistics([]),
        "request_id": response.headers["X-Request-ID"],
    }


@pytest.mark.parametrize(
    "query",
    [
        "group_limit=0",
        "group_limit=101",
        "group_limit=-1",
        "group_limit=%2B1",
        "group_limit=%201",
        "group_limit=1%20",
        "group_limit=1.0",
        "group_limit=1e1",
        "group_limit=01",
        "group_limit=00",
        "group_limit=1000000",
        "group_limit=%EF%BC%91",
        "group_limit=true",
        "group_limit=null",
        "group_limit=",
        "group_limit=1&group_limit=2",
        "group_limit=1&%67roup_limit=2",
        "group_limit[]=1",
        "limit=1",
        "offset=0",
        "metric=height",
        "direction=asc",
        "&".join("q=a" for _ in range(8)),
    ],
)
def test_invalid_statistics_arguments_fail_before_any_database_access(
    express_client, express_headers, tracked_connections, query
):
    response = express_client.get(STATISTICS_URL + "?" + query, headers=express_headers)

    assert_error(response, 400, "invalid_request")
    assert tracked_connections == []


@pytest.mark.parametrize(
    "query", ["group_limit=1", "metric=height", "direction=asc"]
)
def test_search_does_not_accept_analytics_parameters(
    express_client, express_headers, tracked_connections, query
):
    response = express_client.get(PLAYERS_URL + "?" + query, headers=express_headers)

    assert_error(response, 400, "invalid_request")
    assert tracked_connections == []


@pytest.mark.parametrize("metric", ["height", "weight", "birthYear"])
@pytest.mark.parametrize("direction", [None, "asc", "desc"])
def test_rankings_preserve_records_counts_and_page_metadata(
    express_client, express_headers, player_records, expected_rankings,
    caplog, metric, direction
):
    caplog.set_level(logging.INFO)
    query = {"metric": metric, "limit": "1"}
    if direction is not None:
        query["direction"] = direction

    response = express_client.get(
        RANKINGS_URL,
        query_string=query,
        headers={**express_headers, "X-Request-ID": "rankings-123"},
    )

    assert response.status_code == 200
    assert response.is_json
    assert response.get_json() == {
        **expected_rankings(player_records, metric, direction or "desc", limit=1),
        "request_id": "rankings-123",
    }
    assert response.headers["X-Request-ID"] == "rankings-123"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    event = json.loads(caplog.records[-1].getMessage())
    assert event["operation"] == "express_integration.rank_players"
    assert event["route"] == RANKINGS_URL
    assert event["result_count"] == 1


@pytest.mark.parametrize(
    ("query", "indexes"),
    [
        ({}, [0, 1, 2, 3]),
        ({"q": "AARON"}, [0, 1]),
        ({"q": "Henry Louis"}, [0]),
        ({"q": "ren\u00e9"}, [2]),
        ({"q": "REN\u00c9"}, []),
        ({"q": "unknown01"}, [3]),
        ({"q": "' OR 1=1 --"}, []),
        ({"q": "%"}, []),
        ({"q": "_"}, []),
        ({"q": "\\"}, []),
        ({"birth_country": "C\u00f4te d'Ivoire"}, [2]),
        ({"birth_country": "usa"}, []),
        ({"birth_country": "USA' OR 1=1 --"}, []),
        ({"birth_year_min": "1939"}, [1]),
        ({"birth_year_max": "1934"}, [0]),
        ({"birth_year_min": "1", "birth_year_max": "9999"}, [0, 1]),
        ({"bats": "B"}, [2]),
        ({"throws": "S"}, [2]),
        ({"q": "x" * 100, "birth_country": "x" * 100}, []),
        (
            {
                "q": "Aaron",
                "birth_country": "USA",
                "birth_year_min": "1934",
                "birth_year_max": "1939",
                "bats": "R",
                "throws": "R",
                "direction": "asc",
                "limit": "1",
                "offset": "1",
            },
            [0, 1],
        ),
    ],
)
def test_ranking_filters_apply_to_both_rankable_and_excluded_counts(
    express_client, express_headers, player_records, expected_rankings, query, indexes
):
    response = express_client.get(
        RANKINGS_URL, query_string={"metric": "height", **query}, headers=express_headers
    )
    expected = expected_rankings(
        [player_records[index] for index in indexes],
        direction=query.get("direction", "desc"),
        limit=int(query.get("limit", "20")),
        offset=int(query.get("offset", "0")),
    )

    assert response.status_code == 200
    assert response.get_json() == {
        **expected, "request_id": response.headers["X-Request-ID"]
    }
    assert expected["pagination"]["total"] + expected["excluded_count"] == len(indexes)


@pytest.mark.parametrize("q", ["%", "_", "\\"])
def test_ranking_search_matches_literal_wildcards(
    express_client, express_headers, player_db, player_records, expected_rankings, q
):
    record = {**player_records[0], "nameGiven": r"Marker\%_value"}
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            "UPDATE players SET nameGiven = ? WHERE playerId = ?",
            (record["nameGiven"], record["playerId"]),
        )
        conn.commit()

    response = express_client.get(
        RANKINGS_URL,
        query_string={"metric": "height", "q": q},
        headers=express_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {
        **expected_rankings([record]), "request_id": response.headers["X-Request-ID"]
    }


def test_rankings_accept_percent_encoded_enum_and_page_parameters(
    express_client, express_headers, player_records, expected_rankings
):
    response = express_client.get(
        RANKINGS_URL + "?%6detric=%62irthYear&%64irection=%61sc"
        "&%6cimit=%31&%6Fffset=%31&birth_country=USA",
        headers=express_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {
        **expected_rankings(player_records[:2], "birthYear", "asc", limit=1, offset=1),
        "request_id": response.headers["X-Request-ID"],
    }


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"limit": "1"},
        {"limit": "100"},
        {"limit": "1", "offset": "1"},
        {"limit": "100", "offset": "100"},
        {"limit": "100", "offset": "100000"},
    ],
)
def test_rankings_enforce_exact_page_sizes_and_offset_boundaries(
    express_client, express_headers, player_db, player_records, expected_rankings, query
):
    records = [
        {**dict.fromkeys(player_records[0]), "playerId": f"p{index:03d}", "height": 72.0}
        for index in reversed(range(101))
    ]
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.executemany(
            "INSERT INTO players (playerId, height) VALUES (?, ?)",
            [(record["playerId"], record["height"]) for record in records],
        )
        conn.commit()

    response = express_client.get(
        RANKINGS_URL, query_string={"metric": "height", **query}, headers=express_headers
    )

    assert response.status_code == 200
    assert response.get_json() == {
        **expected_rankings(
            records, limit=int(query.get("limit", "20")), offset=int(query.get("offset", "0"))
        ),
        "request_id": response.headers["X-Request-ID"],
    }


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_rankings_keep_binary_ascending_ties_across_http_pages(
    express_client, express_headers, player_db, direction
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.executemany(
            "INSERT INTO players (playerId, height) VALUES (?, ?)",
            [
                ("a", 72), ("Z", 72), ("A", 72), ("z", 72),
                ("tall", 75), ("short", 69), ("missing", None),
            ],
        )
        conn.commit()
    received_ids = []
    for offset in (0, 2, 4):
        response = express_client.get(
            RANKINGS_URL,
            query_string={
                "metric": "height", "direction": direction,
                "limit": "2", "offset": str(offset),
            },
            headers=express_headers,
        )
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["excluded_count"] == 1
        assert payload["pagination"] == {
            "limit": 2, "offset": offset, "total": 6,
            "next_offset": offset + 2 if offset < 4 else None,
        }
        received_ids.extend(row["playerId"] for row in payload["data"])
    assert received_ids == (
        ["short", "A", "Z", "a", "z", "tall"]
        if direction == "asc"
        else ["tall", "A", "Z", "a", "z", "short"]
    )


@pytest.mark.parametrize("metric", ["height", "weight", "birthYear"])
@pytest.mark.parametrize("empty", [False, True])
def test_empty_and_all_null_rankings_are_successful_with_explicit_counts(
    express_client, express_headers, player_db, player_records, expected_rankings,
    metric, empty
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players" if empty else f"UPDATE players SET {metric} = NULL")
        conn.commit()
    records = [] if empty else [{**record, metric: None} for record in player_records]

    response = express_client.get(
        RANKINGS_URL, query_string={"metric": metric}, headers=express_headers
    )

    assert response.status_code == 200
    assert response.get_json() == {
        **expected_rankings(records, metric),
        "request_id": response.headers["X-Request-ID"],
    }


@pytest.mark.parametrize(
    "query",
    [
        "",
        "direction=asc",
        "metric=",
        "metric=HEIGHT",
        "metric=birthyear",
        "metric=homeRuns",
        "metric=age",
        "metric=playerId",
        "metric=height%20",
        "metric=%20height",
        "metric=height%00",
        "metric=%EF%BD%88eight",
        "metric=%2568eight",
        "metric=height+DESC",
        "metric=height%3B+DROP+TABLE+players%3B+--",
        "metric=height&direction=",
        "metric=height&direction=ASC",
        "metric=height&direction=DESC",
        "metric=height&direction=ascending",
        "metric=height&direction=asc%20",
        "metric=height&direction=desc%3B+DROP+TABLE+players%3B+--",
        "metric=height&metric=weight",
        "metric=height&%6detric=weight",
        "metric=height&direction=asc&direction=desc",
        "metric=height&direction=asc&%64irection=desc",
        "metric[]=height",
        "metric=height&direction[]=asc",
        "metric=height&group_limit=1",
        "metric=height&sort=weight",
        "metric=height&sql=SELECT+*+FROM+players",
        "metric=height&player_ids=aaronha01",
        "metric=height&" + "&".join("q=a" for _ in range(10)),
    ],
)
def test_invalid_ranking_arguments_fail_before_any_database_access(
    express_client, express_headers, tracked_connections, query
):
    response = express_client.get(RANKINGS_URL + "?" + query, headers=express_headers)

    assert_error(response, 400, "invalid_request")
    assert tracked_connections == []


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
@pytest.mark.parametrize("url", [PLAYERS_URL, STATISTICS_URL, RANKINGS_URL])
def test_invalid_inputs_fail_before_any_database_read(
    express_client, express_headers, tracked_connections, query, url
):
    if url == RANKINGS_URL:
        query = "metric=height&" + query
    response = express_client.get(
        url + "?" + query, headers=express_headers
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
@pytest.mark.parametrize("url", [PLAYERS_URL, DETAIL_URL, STATISTICS_URL, RANKINGS_URL])
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
@pytest.mark.parametrize("url", READ_URLS)
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
@pytest.mark.parametrize("url", READ_URLS)
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
        (STATISTICS_URL, "POST", 405, "method_not_allowed"),
        (STATISTICS_URL, "DELETE", 405, "method_not_allowed"),
        (STATISTICS_URL + "/", "GET", 404, "not_found"),
        (RANKINGS_URL, "POST", 405, "method_not_allowed"),
        (RANKINGS_URL, "DELETE", 405, "method_not_allowed"),
        (RANKINGS_URL + "/", "GET", 404, "not_found"),
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


@pytest.mark.parametrize("url", READ_URLS)
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
@pytest.mark.parametrize("url", READ_URLS)
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
        (
            STATISTICS_URL, "get_player_statistics",
            {"q": "sensitive-search"}, STATISTICS_URL,
        ),
        (
            RANKINGS_URL, "rank_players",
            {"metric": "height", "q": "sensitive-search"}, RANKINGS_URL,
        ),
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


@pytest.mark.parametrize("url", READ_URLS)
def test_missing_storage_returns_503_without_recreating_it(
    express_client, express_headers, player_db, url
):
    player_db.unlink()
    response = express_client.get(url, headers=express_headers)
    assert_error(response, 503, "storage_unavailable")
    assert not player_db.exists()


@pytest.mark.parametrize("invalid", [float("inf"), -float("inf"), b"not-json"])
@pytest.mark.parametrize("url", READ_URLS)
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


@pytest.mark.parametrize("url", READ_URLS)
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


@pytest.mark.parametrize("url", READ_URLS)
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
            ) or query.startswith(("SELECT COUNT(*) AS total,", "SELECT COUNT(*), COUNT(")):
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
        (STATISTICS_URL, {"q": "Hank"}, "get_player_statistics", STATISTICS_URL),
        (
            RANKINGS_URL, {"metric": "height", "q": "Hank"},
            "rank_players", RANKINGS_URL,
        ),
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


@pytest.mark.parametrize("value", ["not-numeric", 1e308])
def test_invalid_statistics_aggregates_never_become_successful_json(
    express_client, express_headers, player_db, caplog, value
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("UPDATE players SET height = ?", (value,))
        conn.commit()

    response = express_client.get(STATISTICS_URL, headers=express_headers)

    assert_error(response, 503, "storage_unavailable")
    event = json.loads(caplog.records[-1].getMessage())
    assert event["exception_type"] == "DataError"
    assert event["result_count"] == 0
    assert "Infinity" not in response.get_data(as_text=True)
    assert "not-numeric" not in response.get_data(as_text=True)
    assert "not-numeric" not in caplog.text


@pytest.mark.parametrize("field", ["birthCountry", "bats", "throws"])
def test_unserializable_statistics_groups_never_return_partial_results(
    express_client, express_headers, player_db, caplog, field
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            f"UPDATE players SET {field} = ? WHERE playerId = ?",
            (b"private-category", "aaronha01"),
        )
        conn.commit()

    response = express_client.get(STATISTICS_URL, headers=express_headers)

    assert_error(response, 503, "storage_unavailable")
    event = json.loads(caplog.records[-1].getMessage())
    assert event["exception_type"] == "DataError"
    assert event["result_count"] == 0
    assert "private-category" not in response.get_data(as_text=True)
    assert "private-category" not in caplog.text


@pytest.mark.parametrize("field", ["birthCountry", "bats", "throws"])
def test_statistics_group_query_failures_never_return_partial_results(
    express_client, express_headers, monkeypatch, caplog, field
):
    connect = sqlite3.connect

    class FailingGroupConnection(sqlite3.Connection):
        def execute(self, query, parameters=()):
            if query.startswith(f"SELECT {field} AS value"):
                raise sqlite3.OperationalError("private statistics group diagnostic")
            return super().execute(query, parameters)

    def failing_connect(*args, **kwargs):
        return connect(*args, factory=FailingGroupConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", failing_connect)
    response = express_client.get(STATISTICS_URL, headers=express_headers)

    assert_error(response, 503, "storage_unavailable")
    event = json.loads(caplog.records[-1].getMessage())
    assert event["exception_type"] == "OperationalError"
    assert event["result_count"] == 0
    assert "private statistics" not in response.get_data(as_text=True)
    assert "private statistics" not in caplog.text


@pytest.mark.parametrize("metric", ["height", "weight", "birthYear"])
@pytest.mark.parametrize("value", [float("inf"), -float("inf"), "private-value", b"private"])
@pytest.mark.parametrize("offset", [0, 100000])
def test_invalid_ranking_metrics_outside_the_page_still_fail_safely(
    express_client, express_headers, player_db, caplog, metric, value, offset
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            f"UPDATE players SET {metric} = ? WHERE playerId = ?", (value, "unknown01")
        )
        conn.commit()

    response = express_client.get(
        RANKINGS_URL,
        query_string={"metric": metric, "limit": "1", "offset": str(offset)},
        headers=express_headers,
    )

    assert_error(response, 503, "storage_unavailable")
    event = json.loads(caplog.records[-1].getMessage())
    assert event["exception_type"] == "DataError"
    assert event["result_count"] == 0
    for sensitive in ("Infinity", "private", "unknown01"):
        assert sensitive not in response.get_data(as_text=True)
        assert sensitive not in caplog.text


def test_invalid_ranking_metrics_outside_filters_do_not_block_valid_results(
    express_client, express_headers, player_db, player_records, expected_rankings
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            "UPDATE players SET height = ? WHERE playerId = ?", ("invalid", "unknown01")
        )
        conn.commit()

    response = express_client.get(
        RANKINGS_URL,
        query_string={"metric": "height", "q": "Aaron"},
        headers=express_headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {
        **expected_rankings(player_records[:2]),
        "request_id": response.headers["X-Request-ID"],
    }


@pytest.mark.parametrize(
    ("field", "value"), [("nameGiven", b"private-record"), ("weight", float("inf"))]
)
def test_unserializable_ranking_fields_never_return_partial_results(
    express_client, express_headers, player_db, caplog, field, value
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            f"UPDATE players SET {field} = ? WHERE playerId = ?", (value, "aaronha01")
        )
        conn.commit()

    response = express_client.get(HEIGHT_RANKINGS_URL, headers=express_headers)

    assert_error(response, 503, "storage_unavailable")
    event = json.loads(caplog.records[-1].getMessage())
    assert event["exception_type"] == "DataError"
    assert event["result_count"] == 0
    assert "private-record" not in caplog.text
    assert "private-record" not in response.get_data(as_text=True)
    assert "Infinity" not in response.get_data(as_text=True)


def test_ranking_page_failure_never_returns_partial_metadata(
    express_client, express_headers, monkeypatch, caplog
):
    connect = sqlite3.connect

    class FailingPageConnection(sqlite3.Connection):
        def execute(self, query, parameters=()):
            if query.startswith("SELECT * FROM players WHERE height IS NOT NULL"):
                raise sqlite3.OperationalError("private ranking page diagnostic")
            return super().execute(query, parameters)

    def failing_connect(*args, **kwargs):
        return connect(*args, factory=FailingPageConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", failing_connect)
    response = express_client.get(HEIGHT_RANKINGS_URL, headers=express_headers)

    assert_error(response, 503, "storage_unavailable")
    event = json.loads(caplog.records[-1].getMessage())
    assert event["exception_type"] == "OperationalError"
    assert event["result_count"] == 0
    assert "private ranking" not in caplog.text
    assert "private ranking" not in response.get_data(as_text=True)


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


@pytest.mark.parametrize("group_limit", [None, 1, 100])
def test_seed_statistics_match_independent_csv_aggregates(
    express_client, express_headers, seed_player_records, expected_statistics, group_limit
):
    query = {} if group_limit is None else {"group_limit": str(group_limit)}
    response = express_client.get(
        STATISTICS_URL, query_string=query, headers=express_headers
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload == {
        **expected_statistics(
            seed_player_records, 20 if group_limit is None else group_limit
        ),
        "request_id": response.headers["X-Request-ID"],
    }
    assert payload["total"] == 19370
    assert payload["numeric"]["birthYear"]["missing_count"] == 126
    assert payload["numeric"]["height"]["missing_count"] == 755
    assert payload["numeric"]["weight"]["missing_count"] == 837
    assert payload["groups"]["birthCountry"]["total_groups"] == 55
    assert payload["groups"]["birthCountry"]["missing_count"] == 68


def test_filtered_seed_statistics_match_independent_csv_population(
    express_client, express_headers, seed_player_records, expected_statistics
):
    records = [
        record
        for record in seed_player_records
        if record["birthCountry"] == "USA"
        and record["birthYear"] is not None
        and 1930 <= float(record["birthYear"]) <= 1940
        and record["bats"] == "R"
        and record["throws"] == "R"
    ]
    assert len(records) > 20
    response = express_client.get(
        STATISTICS_URL,
        headers=express_headers,
        query_string={
            "birth_country": "USA",
            "birth_year_min": "1930",
            "birth_year_max": "1940",
            "bats": "R",
            "throws": "R",
            "group_limit": "1",
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {
        **expected_statistics(records, group_limit=1),
        "request_id": response.headers["X-Request-ID"],
    }


@pytest.mark.parametrize(
    ("metric", "missing"), [("height", 755), ("weight", 837), ("birthYear", 126)]
)
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_complete_seed_rankings_through_authenticated_http(
    express_client, express_headers, seed_player_records, expected_rankings,
    metric, missing, direction
):
    assert len(seed_player_records) == 19370
    numeric_columns = (
        "birthYear", "birthMonth", "birthDay", "deathYear", "deathMonth", "deathDay",
        "height", "weight",
    )
    records = [
        {
            column: (
                float(value) if column in numeric_columns and value is not None else value
            )
            for column, value in record.items()
        }
        for record in seed_player_records
    ]
    expected = expected_rankings(records, metric, direction, limit=len(records))
    expected_rows = expected["data"]
    total = 19370 - missing
    assert len(expected_rows) == total
    received_ids = []
    offset = 0
    while offset is not None:
        response = express_client.get(
            RANKINGS_URL,
            query_string={
                "metric": metric, "direction": direction,
                "limit": "100", "offset": str(offset),
            },
            headers=express_headers,
        )
        next_offset = offset + 100 if offset + 100 < total else None
        assert response.status_code == 200
        payload = response.get_json()
        assert payload == {
            "metric": metric,
            "direction": direction,
            "excluded_count": missing,
            "data": expected_rows[offset : offset + 100],
            "pagination": {
                "limit": 100, "offset": offset, "total": total,
                "next_offset": next_offset,
            },
            "request_id": response.headers["X-Request-ID"],
        }
        received_ids.extend(row["playerId"] for row in payload["data"])
        offset = next_offset
    assert received_ids == [row["playerId"] for row in expected_rows]
    assert len(set(received_ids)) == total


def test_filtered_seed_rankings_match_independent_csv_population(
    express_client, express_headers, seed_player_records, expected_rankings
):
    records = [
        record
        for record in seed_player_records
        if record["birthCountry"] == "USA"
        and record["birthYear"] is not None
        and 1930 <= float(record["birthYear"]) <= 1940
        and record["bats"] == "R"
        and record["throws"] == "R"
    ]
    assert len(records) > 100
    expected = expected_rankings(records, "weight", "asc", limit=7, offset=10)

    response = express_client.get(
        RANKINGS_URL,
        headers=express_headers,
        query_string={
            "metric": "weight",
            "direction": "asc",
            "birth_country": "USA",
            "birth_year_min": "1930",
            "birth_year_max": "1940",
            "bats": "R",
            "throws": "R",
            "limit": "7",
            "offset": "10",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["metric"] == "weight"
    assert payload["direction"] == "asc"
    assert payload["excluded_count"] == expected["excluded_count"]
    assert payload["pagination"] == expected["pagination"]
    assert [row["playerId"] for row in payload["data"]] == [
        row["playerId"] for row in expected["data"]
    ]
    assert [row["weight"] for row in payload["data"]] == [
        float(row["weight"]) for row in expected["data"]
    ]


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
