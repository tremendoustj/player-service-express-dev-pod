import json
import sqlite3
from contextlib import closing

import pytest

from player_service import PlayerFilters, PlayerService


@pytest.fixture
def service(player_db):
    return PlayerService(player_db)


def test_default_rankings_preserve_records_and_report_missing_values(
    service, player_records, expected_rankings
):
    result = service.rank_players("height")

    assert result == expected_rankings(player_records)
    assert result["excluded_count"] == 2
    assert result["pagination"]["total"] == 2
    json.dumps(result, allow_nan=False)
    assert service.get_all_players() == player_records


@pytest.mark.parametrize("metric", ["height", "weight", "birthYear"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_each_metric_supports_both_directions(
    service, player_records, expected_rankings, metric, direction
):
    assert service.rank_players(metric, direction=direction) == expected_rankings(
        player_records, metric, direction
    )


@pytest.mark.parametrize("direction", ["asc", "desc"])
@pytest.mark.parametrize("reverse", [False, True])
def test_ties_use_binary_player_id_order_across_pages(
    service, player_db, direction, reverse
):
    rows = [
        ("a", 72),
        ("Z", 72),
        ("A", 72),
        ("z", 72),
        ("tall", 75),
        ("short", 69),
        ("missing", None),
    ]
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.executemany(
            "INSERT INTO players (playerId, height) VALUES (?, ?)",
            list(reversed(rows)) if reverse else rows,
        )
        conn.commit()
    expected_ids = (
        ["short", "A", "Z", "a", "z", "tall"]
        if direction == "asc"
        else ["tall", "A", "Z", "a", "z", "short"]
    )

    actual_ids = []
    for offset in (0, 2, 4):
        page = service.rank_players("height", direction=direction, limit=2, offset=offset)
        actual_ids.extend(row["playerId"] for row in page["data"])
        assert page["excluded_count"] == 1
        assert page["pagination"] == {
            "limit": 2,
            "offset": offset,
            "total": 6,
            "next_offset": offset + 2 if offset < 4 else None,
        }
    assert actual_ids == expected_ids


@pytest.mark.parametrize(
    ("filters", "record_indexes"),
    [
        ({}, [0, 1, 2, 3]),
        ({"q": "AARON"}, [0, 1]),
        ({"q": "ha01"}, [0]),
        ({"q": "Hank Aaron"}, [0]),
        ({"q": "Henry Louis"}, [0]),
        ({"q": "ren\u00e9"}, [2]),
        ({"q": "REN\u00c9"}, []),
        ({"q": "missing"}, []),
        ({"q": "' OR 1=1 --"}, []),
        ({"birth_country": "USA"}, [0, 1]),
        ({"birth_country": "usa"}, []),
        ({"birth_country": "C\u00f4te d'Ivoire"}, [2]),
        ({"birth_country": "USA' OR 1=1 --"}, []),
        ({"birth_country": "USA'; DROP TABLE players; --"}, []),
        ({"birth_year_min": 1939}, [1]),
        ({"birth_year_max": 1934}, [0]),
        ({"birth_year_min": 1934, "birth_year_max": 1934}, [0]),
        ({"bats": "B"}, [2]),
        ({"throws": "S"}, [2]),
        (
            {
                "q": "Aaron",
                "birth_country": "USA",
                "birth_year_min": 1939,
                "birth_year_max": 1950,
                "bats": "R",
                "throws": "R",
            },
            [1],
        ),
    ],
)
def test_filters_apply_to_rankings_and_exclusions(
    service, player_records, expected_rankings, filters, record_indexes
):
    records = [player_records[index] for index in record_indexes]

    assert service.rank_players("height", PlayerFilters(**filters)) == expected_rankings(
        records
    )
    assert service.get_all_players() == player_records


@pytest.mark.parametrize("q", ["%", "_", "\\"])
def test_ranking_search_treats_wildcards_literally(
    service, player_db, player_records, expected_rankings, q
):
    record = {**player_records[1], "nameGiven": r"Marker\%_value"}
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            "UPDATE players SET nameGiven = ? WHERE playerId = ?",
            (record["nameGiven"], record["playerId"]),
        )
        conn.commit()

    assert service.rank_players("height", PlayerFilters(q=q)) == expected_rankings([record])


@pytest.mark.parametrize(
    ("limit", "offset"),
    [(1, 0), (1, 1), (2, 0), (2, 1), (100, 0), (20, 2), (20, 100000)],
)
def test_page_totals_and_exclusions_do_not_depend_on_page(
    service, player_records, expected_rankings, limit, offset
):
    assert service.rank_players("height", limit=limit, offset=offset) == expected_rankings(
        player_records, limit=limit, offset=offset
    )


def test_empty_population_returns_an_explicit_empty_page(
    service, player_db, expected_rankings
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.commit()

    assert service.rank_players("height") == expected_rankings([])


@pytest.mark.parametrize("metric", ["height", "weight", "birthYear"])
def test_all_null_metrics_are_excluded_not_zeroed(service, player_db, metric):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(f"UPDATE players SET {metric} = NULL")
        conn.commit()

    page = service.rank_players(metric)

    assert page["data"] == []
    assert page["excluded_count"] == 4
    assert page["pagination"] == {
        "limit": 20,
        "offset": 0,
        "total": 0,
        "next_offset": None,
    }
    json.dumps(page, allow_nan=False)


@pytest.mark.parametrize("metric", ["height", "weight", "birthYear"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_zero_and_fractional_measurements_remain_numeric(
    service, player_db, metric, direction
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.executemany(
            f"INSERT INTO players (playerId, {metric}) VALUES (?, ?)",
            [("zero", 0), ("negative", -1.5), ("positive", 2.5), ("missing", None)],
        )
        conn.commit()

    page = service.rank_players(metric, direction=direction)

    assert [row[metric] for row in page["data"]] == (
        [-1.5, 0.0, 2.5] if direction == "asc" else [2.5, 0.0, -1.5]
    )
    assert page["excluded_count"] == 1
    assert page["pagination"]["total"] == 3


def test_finite_values_do_not_require_a_nonoverflowing_mean(service, player_db):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("UPDATE players SET height = ?", (1e308,))
        conn.commit()

    page = service.rank_players("height")

    assert [row["height"] for row in page["data"]] == [1e308] * 4
    assert page["excluded_count"] == 0
    json.dumps(page, allow_nan=False)


@pytest.mark.parametrize(
    "metric",
    [
        "homeRuns",
        "playerId",
        "HEIGHT",
        "height ",
        "",
        None,
        1,
        True,
        [],
        {},
        "height DESC",
        "height; DROP TABLE players; --",
    ],
)
def test_invalid_metric_fails_before_querying(service, monkeypatch, metric):
    def unexpected_connection():
        pytest.fail("Invalid metrics must not open a database connection")

    monkeypatch.setattr(service, "_read_connection", unexpected_connection)

    with pytest.raises(ValueError, match="metric must be one of height, weight, birthYear"):
        service.rank_players(metric)


@pytest.mark.parametrize(
    "direction",
    [
        "ASC",
        "DESC",
        "asc ",
        "",
        None,
        1,
        True,
        [],
        {},
        "desc; DROP TABLE players; --",
    ],
)
def test_invalid_direction_fails_before_querying(service, monkeypatch, direction):
    def unexpected_connection():
        pytest.fail("Invalid directions must not open a database connection")

    monkeypatch.setattr(service, "_read_connection", unexpected_connection)

    with pytest.raises(ValueError, match="direction must be one of asc, desc"):
        service.rank_players("height", direction=direction)


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": 101},
        {"limit": True},
        {"limit": 1.0},
        {"limit": "20"},
        {"limit": None},
        {"offset": -1},
        {"offset": 100001},
        {"offset": True},
        {"offset": 0.0},
        {"offset": "0"},
        {"offset": None},
    ],
)
def test_invalid_pagination_fails_before_querying(service, monkeypatch, arguments):
    def unexpected_connection():
        pytest.fail("Invalid pagination must not open a database connection")

    monkeypatch.setattr(service, "_read_connection", unexpected_connection)

    with pytest.raises(ValueError, match="must be an integer between"):
        service.rank_players("height", **arguments)


@pytest.mark.parametrize("filters", [{}, [], "", 1, True])
def test_rankings_require_validated_filters_before_querying(service, monkeypatch, filters):
    def unexpected_connection():
        pytest.fail("Invalid filters must not open a database connection")

    monkeypatch.setattr(service, "_read_connection", unexpected_connection)

    with pytest.raises(TypeError, match="filters must be a PlayerFilters instance"):
        service.rank_players("height", filters)


@pytest.mark.parametrize("metric", ["height", "weight", "birthYear"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), "not-numeric", b"invalid"])
@pytest.mark.parametrize("offset", [0, 100000])
def test_invalid_metrics_anywhere_in_population_raise(
    service, player_db, metric, value, offset
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            f"UPDATE players SET {metric} = ? WHERE playerId = ?", (value, "unknown01")
        )
        conn.commit()

    with pytest.raises(
        sqlite3.DataError, match=f"{metric} rankings require finite numeric values"
    ):
        service.rank_players(metric, limit=1, offset=offset)


def test_invalid_metrics_outside_filtered_population_do_not_block_rankings(
    service, player_db, player_records, expected_rankings
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            "UPDATE players SET height = ? WHERE playerId = ?", ("invalid", "unknown01")
        )
        conn.commit()

    assert service.rank_players("height", PlayerFilters(q="Aaron")) == expected_rankings(
        player_records[:2]
    )


def test_counts_validation_and_page_share_one_snapshot(
    service, player_db, player_records, monkeypatch, expected_rankings
):
    with closing(sqlite3.connect(player_db)) as conn:
        assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    connect = sqlite3.connect

    class ConcurrentWriteConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith("SELECT * FROM players"):
                with closing(connect(player_db)) as writer:
                    writer.executemany(
                        "INSERT INTO players (playerId, height) VALUES (?, ?)",
                        [("late001", 90), ("late002", None)],
                    )
                    writer.execute(
                        "UPDATE players SET height = ? WHERE playerId = ?",
                        (float("inf"), "aaronha01"),
                    )
                    writer.commit()
            return super().execute(sql, parameters)

    def connect_with_writer(*args, **kwargs):
        return connect(*args, factory=ConcurrentWriteConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_with_writer)

    assert service.rank_players("height") == expected_rankings(player_records)
    with closing(connect(player_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 6
        assert conn.execute(
            "SELECT height FROM players WHERE playerId = ?", ("aaronha01",)
        ).fetchone()[0] == float("inf")


def test_default_and_maximum_page_sizes_are_exact(service, player_db):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.executemany(
            "INSERT INTO players (playerId, height) VALUES (?, ?)",
            [(f"p{index:03d}", 70) for index in reversed(range(101))],
        )
        conn.commit()

    default_page = service.rank_players("height")
    assert len(default_page["data"]) == 20
    assert default_page["pagination"] == {
        "limit": 20,
        "offset": 0,
        "total": 101,
        "next_offset": 20,
    }
    maximum_page = service.rank_players("height", limit=100)
    assert [row["playerId"] for row in maximum_page["data"]] == [
        f"p{index:03d}" for index in range(100)
    ]
    assert maximum_page["pagination"] == {
        "limit": 100,
        "offset": 0,
        "total": 101,
        "next_offset": 100,
    }


def test_next_offset_never_exceeds_supported_cap(service, player_db):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.execute(
            """
            WITH RECURSIVE numbers(n) AS (
                VALUES(0)
                UNION ALL SELECT n + 1 FROM numbers WHERE n < 100004
            )
            INSERT INTO players (playerId, height)
            SELECT printf('p%06d', n), 70 FROM numbers
            """
        )
        conn.commit()

    assert service.rank_players("height", limit=1, offset=99999)["pagination"] == {
        "limit": 1,
        "offset": 99999,
        "total": 100005,
        "next_offset": 100000,
    }
    page = service.rank_players("height", limit=1, offset=100000)
    assert [row["playerId"] for row in page["data"]] == ["p100000"]
    assert page["pagination"] == {
        "limit": 1,
        "offset": 100000,
        "total": 100005,
        "next_offset": None,
    }
    crossing_page = service.rank_players("height", limit=2, offset=99999)
    assert crossing_page["pagination"]["next_offset"] is None


def assert_complete_rankings(
    service, records, metric, direction, expected_rankings, filters=None
):
    expected = expected_rankings(records, metric, direction, limit=len(records))
    expected_rows = expected["data"]
    actual_ids = []
    offset = 0
    while True:
        page = service.rank_players(
            metric, filters, direction=direction, limit=100, offset=offset
        )
        assert set(page) == {"metric", "direction", "excluded_count", "data", "pagination"}
        assert page["metric"] == metric
        assert page["direction"] == direction
        assert page["excluded_count"] == expected["excluded_count"]
        next_offset = offset + 100 if offset + 100 < len(expected_rows) else None
        assert page["pagination"] == {
            "limit": 100,
            "offset": offset,
            "total": len(expected_rows),
            "next_offset": next_offset,
        }
        expected_page = expected_rows[offset : offset + 100]
        assert [row["playerId"] for row in page["data"]] == [
            row["playerId"] for row in expected_page
        ]
        assert [row[metric] for row in page["data"]] == [
            float(row[metric]) for row in expected_page
        ]
        json.dumps(page, allow_nan=False)
        actual_ids.extend(row["playerId"] for row in page["data"])
        if next_offset is None:
            break
        offset = next_offset
    assert actual_ids == [row["playerId"] for row in expected_rows]
    assert len(actual_ids) == len(set(actual_ids))


@pytest.mark.parametrize(
    ("metric", "missing"), [("height", 755), ("weight", 837), ("birthYear", 126)]
)
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_complete_repository_seed_rankings(
    service, seed_player_records, expected_rankings, metric, missing, direction
):
    assert len(seed_player_records) == 19370
    assert sum(record[metric] is None for record in seed_player_records) == missing

    assert_complete_rankings(
        service, seed_player_records, metric, direction, expected_rankings
    )


def test_filtered_seed_rankings_match_independent_population(
    service, seed_player_records, expected_rankings
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

    assert_complete_rankings(
        service,
        records,
        "height",
        "desc",
        expected_rankings,
        PlayerFilters(
            birth_country="USA",
            birth_year_min=1930,
            birth_year_max=1940,
            bats="R",
            throws="R",
        ),
    )
