import json
import sqlite3
from contextlib import closing

import pytest

from player_service import PlayerFilters, PlayerService


@pytest.fixture
def service(player_db):
    return PlayerService(player_db)


def test_statistics_preserve_nulls_and_use_only_known_measurements(
    service, player_records, expected_statistics
):
    result = service.get_player_statistics()

    assert result == expected_statistics(player_records)
    assert result["numeric"]["height"] == {
        "known_count": 2,
        "missing_count": 2,
        "min": 72.0,
        "max": 75.0,
        "mean": 73.5,
    }
    assert result["groups"]["throws"]["data"] == [
        {"value": "R", "count": 2},
        {"value": "S", "count": 1},
    ]
    json.dumps(result, allow_nan=False)
    assert service.get_all_players() == player_records


@pytest.mark.parametrize(
    ("filters", "record_indexes"),
    [
        ({}, [0, 1, 2, 3]),
        ({"q": "AARON"}, [0, 1]),
        ({"q": "Henry Louis"}, [0]),
        ({"q": "ren\u00e9"}, [2]),
        ({"q": "REN\u00c9"}, []),
        ({"q": "unknown01"}, [3]),
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
def test_statistics_apply_shared_filters_to_every_aggregate(
    service, player_records, expected_statistics, filters, record_indexes
):
    result = service.get_player_statistics(PlayerFilters(**filters), group_limit=1)

    assert result == expected_statistics(
        [player_records[index] for index in record_indexes], group_limit=1
    )
    assert service.get_all_players() == player_records


@pytest.mark.parametrize("q", ["%", "_", "\\"])
def test_statistics_search_is_literal(
    service, player_db, player_records, expected_statistics, q
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            "UPDATE players SET nameGiven = ? WHERE playerId = ?",
            (r"Marker\%_value", "unknown01"),
        )
        conn.commit()

    assert service.get_player_statistics(PlayerFilters(q=q)) == expected_statistics(
        [player_records[3]]
    )


def test_empty_population_has_explicit_zero_counts_and_null_measures(
    service, player_db, expected_statistics
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.commit()

    result = service.get_player_statistics()

    assert result == expected_statistics([])
    json.dumps(result, allow_nan=False)


def test_zero_measurements_are_known_not_missing(service, player_db):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("UPDATE players SET birthYear = 0, height = 0, weight = 0")
        conn.commit()

    result = service.get_player_statistics()

    for measures in result["numeric"].values():
        assert measures == {
            "known_count": 4,
            "missing_count": 0,
            "min": 0.0,
            "max": 0.0,
            "mean": 0.0,
        }


def test_group_limit_does_not_limit_the_population_or_hide_omitted_counts(
    service, player_records, expected_statistics
):
    result = service.get_player_statistics(group_limit=1)

    assert result == expected_statistics(player_records, group_limit=1)
    assert result["total"] == 4
    assert result["groups"]["birthCountry"] == {
        "data": [{"value": "USA", "count": 2}],
        "known_count": 3,
        "missing_count": 1,
        "total_groups": 2,
        "limit": 1,
        "other_count": 1,
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_group_order_uses_counts_then_binary_values_and_keeps_nulls_separate(
    service, player_db, reverse
):
    countries = ["a", "Z", "A", "a", "", "Unknown", None]
    if reverse:
        countries.reverse()
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.executemany(
            "INSERT INTO players (birthCountry) VALUES (?)",
            [(country,) for country in countries],
        )
        conn.commit()

    assert service.get_player_statistics()["groups"]["birthCountry"] == {
        "data": [
            {"value": "a", "count": 2},
            {"value": "", "count": 1},
            {"value": "A", "count": 1},
            {"value": "Unknown", "count": 1},
            {"value": "Z", "count": 1},
        ],
        "known_count": 6,
        "missing_count": 1,
        "total_groups": 5,
        "limit": 20,
        "other_count": 0,
    }


def test_maximum_group_limit_returns_exactly_100_groups(service, player_db):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.executemany(
            "INSERT INTO players (birthCountry) VALUES (?)",
            [(f"C{index:03d}",) for index in reversed(range(101))],
        )
        conn.commit()

    result = service.get_player_statistics(group_limit=100)

    assert result["total"] == 101
    assert result["groups"]["birthCountry"] == {
        "data": [{"value": f"C{index:03d}", "count": 1} for index in range(100)],
        "known_count": 101,
        "missing_count": 0,
        "total_groups": 101,
        "limit": 100,
        "other_count": 1,
    }


@pytest.mark.parametrize("group_limit", [0, -1, 101, True, False, 1.0, "20", None])
def test_invalid_group_limit_fails_before_querying(service, monkeypatch, group_limit):
    def unexpected_connection():
        pytest.fail("Invalid group limits must not open a database connection")

    monkeypatch.setattr(service, "_read_connection", unexpected_connection)

    with pytest.raises(ValueError, match="group_limit must be an integer between 1 and 100"):
        service.get_player_statistics(group_limit=group_limit)


@pytest.mark.parametrize("filters", [{}, [], "", 1, True])
def test_statistics_require_validated_filters_before_querying(
    service, monkeypatch, filters
):
    def unexpected_connection():
        pytest.fail("Invalid filters must not open a database connection")

    monkeypatch.setattr(service, "_read_connection", unexpected_connection)

    with pytest.raises(TypeError, match="filters must be a PlayerFilters instance"):
        service.get_player_statistics(filters)


@pytest.mark.parametrize("field", ["birthYear", "height", "weight"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), "not-numeric", b"invalid"])
def test_invalid_numeric_storage_raises_instead_of_returning_misleading_statistics(
    service, player_db, field, value
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            f"UPDATE players SET {field} = ? WHERE playerId = ?", (value, "unknown01")
        )
        conn.commit()

    with pytest.raises(
        sqlite3.DataError, match=f"{field} statistics require finite numeric values"
    ):
        service.get_player_statistics()


def test_overflowing_mean_raises_instead_of_returning_infinity(service, player_db):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("UPDATE players SET height = ?", (1e308,))
        conn.commit()

    with pytest.raises(sqlite3.DataError, match="height statistics"):
        service.get_player_statistics()


@pytest.mark.parametrize("field", ["birthCountry", "bats", "throws"])
def test_all_statistics_queries_share_one_snapshot(
    service, player_db, player_records, expected_statistics, monkeypatch, field
):
    with closing(sqlite3.connect(player_db)) as conn:
        assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    connect = sqlite3.connect

    class ConcurrentWriteConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith(f"SELECT {field} AS value"):
                with closing(connect(player_db)) as writer:
                    writer.execute(
                        "INSERT INTO players "
                        "(playerId, birthYear, height, weight, birthCountry, bats, throws) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        ("late001", 2000, 80, 220, "CAN", "L", "L"),
                    )
                    writer.commit()
            return super().execute(sql, parameters)

    def connect_with_writer(*args, **kwargs):
        return connect(*args, factory=ConcurrentWriteConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_with_writer)

    assert service.get_player_statistics() == expected_statistics(player_records)
    with closing(connect(player_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 5


@pytest.mark.parametrize("group_limit", [20, 100])
def test_complete_repository_seed_statistics(
    service, seed_player_records, expected_statistics, group_limit
):
    result = service.get_player_statistics(group_limit=group_limit)

    assert result == expected_statistics(seed_player_records, group_limit)
    assert result["total"] == 19370
    assert result["numeric"]["birthYear"]["missing_count"] == 126
    assert result["numeric"]["height"]["missing_count"] == 755
    assert result["numeric"]["weight"]["missing_count"] == 837
    assert result["groups"]["birthCountry"]["total_groups"] == 55
    assert result["groups"]["birthCountry"]["missing_count"] == 68
    json.dumps(result, allow_nan=False)


def test_filtered_seed_statistics_match_independent_population(
    service, seed_player_records, expected_statistics
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

    result = service.get_player_statistics(
        PlayerFilters(
            birth_country="USA",
            birth_year_min=1930,
            birth_year_max=1940,
            bats="R",
            throws="R",
        )
    )

    assert result == expected_statistics(records)
