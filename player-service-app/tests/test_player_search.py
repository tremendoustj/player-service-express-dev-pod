import sqlite3
from contextlib import closing

import pytest

from player_service import PlayerFilters, PlayerService


@pytest.fixture
def service(player_db):
    return PlayerService(player_db)


def test_default_search_preserves_all_fields_and_nulls(service, player_records):
    assert service.search_players() == {
        "data": player_records,
        "pagination": {"limit": 20, "offset": 0, "total": 4, "next_offset": None},
    }


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
        ({"q": "O'NeIL"}, [2]),
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
        ({"birth_year_min": 1, "birth_year_max": 9999}, [0, 1]),
        ({"birth_year_min": 2000}, []),
        ({"bats": "B"}, [2]),
        ({"bats": "L"}, []),
        ({"throws": "S"}, [2]),
        ({"throws": "R"}, [0, 1]),
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
        ({"q": "Aaron", "throws": "S"}, []),
    ],
)
def test_search_filters_share_exact_population(
    service, player_records, filters, record_indexes
):
    page = service.search_players(PlayerFilters(**filters))

    assert page == {
        "data": [player_records[index] for index in record_indexes],
        "pagination": {
            "limit": 20,
            "offset": 0,
            "total": len(record_indexes),
            "next_offset": None,
        },
    }
    assert service.get_all_players() == player_records


@pytest.mark.parametrize("q", ["%", "_", "\\", "\\%_", "%_"])
def test_search_treats_wildcards_and_escape_character_literally(service, player_db, q):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            "UPDATE players SET nameGiven = ? WHERE playerId = ?",
            (r"Marker\%_value", "unknown01"),
        )
        conn.commit()

    page = service.search_players(PlayerFilters(q=q))

    assert [row["playerId"] for row in page["data"]] == ["unknown01"]
    assert page["pagination"]["total"] == 1


@pytest.mark.parametrize(
    ("limit", "offset", "record_indexes", "next_offset"),
    [
        (1, 0, [0], 1),
        (1, 3, [3], None),
        (2, 0, [0, 1], 2),
        (2, 1, [1, 2], 3),
        (2, 2, [2, 3], None),
        (3, 0, [0, 1, 2], 3),
        (3, 3, [3], None),
        (100, 0, [0, 1, 2, 3], None),
        (20, 4, [], None),
        (20, 100000, [], None),
    ],
)
def test_pagination_boundaries(
    service, player_records, limit, offset, record_indexes, next_offset
):
    assert service.search_players(limit=limit, offset=offset) == {
        "data": [player_records[index] for index in record_indexes],
        "pagination": {
            "limit": limit,
            "offset": offset,
            "total": 4,
            "next_offset": next_offset,
        },
    }


def test_filtered_total_does_not_depend_on_page(service, player_records):
    filters = PlayerFilters(q="Aaron", birth_country="USA")

    assert service.search_players(filters, limit=1, offset=1) == {
        "data": [player_records[1]],
        "pagination": {"limit": 1, "offset": 1, "total": 2, "next_offset": None},
    }
    assert service.search_players(filters, limit=1, offset=2) == {
        "data": [],
        "pagination": {"limit": 1, "offset": 2, "total": 2, "next_offset": None},
    }


def test_search_order_is_independent_of_insertion_order(service, player_db, player_records):
    placeholders = ", ".join("?" for _ in service.columns)
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.executemany(
            f"INSERT INTO players VALUES ({placeholders})",
            [tuple(record.values()) for record in reversed(player_records)],
        )
        conn.commit()

    pages = [service.search_players(limit=1, offset=index) for index in range(4)]

    assert [page["data"][0] for page in pages] == player_records


def test_empty_population_returns_an_explicit_empty_page(service, player_db):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.commit()

    assert service.search_players() == {
        "data": [],
        "pagination": {"limit": 20, "offset": 0, "total": 0, "next_offset": None},
    }


@pytest.mark.parametrize(
    "filters",
    [
        {"q": ""},
        {"q": "   "},
        {"q": "a" * 101},
        {"q": "line\nbreak"},
        {"q": "\x00"},
        {"q": b"Aaron"},
        {"q": 1},
        {"birth_country": ""},
        {"birth_country": " "},
        {"birth_country": "\u00e9" * 101},
        {"birth_country": "USA\t"},
        {"birth_country": ["USA"]},
        {"birth_year_min": 0},
        {"birth_year_max": 10000},
        {"birth_year_min": True},
        {"birth_year_max": False},
        {"birth_year_min": 1934.0},
        {"birth_year_max": "1934"},
        {"birth_year_min": 1940, "birth_year_max": 1939},
        {"bats": ""},
        {"bats": "r"},
        {"bats": "S"},
        {"bats": ["R"]},
        {"throws": ""},
        {"throws": "l"},
        {"throws": "B"},
        {"throws": 1},
        {"throws": "R' OR 1=1 --"},
    ],
)
def test_invalid_filters_raise_without_querying(filters):
    with pytest.raises(ValueError):
        PlayerFilters(**filters)


@pytest.mark.parametrize("field", ["q", "birth_country"])
def test_filter_text_accepts_exact_character_limit(service, field):
    page = service.search_players(PlayerFilters(**{field: "\u00e9" * 100}))

    assert page["data"] == []
    assert page["pagination"]["total"] == 0


@pytest.mark.parametrize(
    "page_arguments",
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
def test_invalid_page_arguments_raise_before_opening_connection(
    service, monkeypatch, page_arguments
):
    def unexpected_connection():
        pytest.fail("Invalid pagination must not open a database connection")

    monkeypatch.setattr(service, "_read_connection", unexpected_connection)

    with pytest.raises(ValueError):
        service.search_players(**page_arguments)


@pytest.mark.parametrize("filters", [{}, [], "", 1, True])
def test_search_requires_validated_filter_type(service, filters):
    with pytest.raises(TypeError, match="filters must be a PlayerFilters instance"):
        service.search_players(filters)


def test_total_and_page_use_one_snapshot(service, player_db, player_records, monkeypatch):
    with closing(sqlite3.connect(player_db)) as conn:
        assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    connect = sqlite3.connect

    class ConcurrentWriteConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith("SELECT * FROM players"):
                with closing(connect(player_db)) as writer:
                    writer.execute(
                        "INSERT INTO players (playerId) VALUES (?)", ("aaaaaa00",)
                    )
                    writer.commit()
            return super().execute(sql, parameters)

    def connect_with_writer(*args, **kwargs):
        return connect(*args, factory=ConcurrentWriteConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_with_writer)

    page = service.search_players()

    assert page["data"] == player_records
    assert page["pagination"]["total"] == 4
    with closing(connect(player_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 5


def test_next_offset_never_exceeds_supported_cap(service, player_db):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.execute(
            """
            WITH RECURSIVE numbers(n) AS (
                VALUES(0)
                UNION ALL SELECT n + 1 FROM numbers WHERE n < 100004
            )
            INSERT INTO players (playerId)
            SELECT printf('p%06d', n) FROM numbers
            """
        )
        conn.commit()

    assert service.search_players(limit=1, offset=99999)["pagination"] == {
        "limit": 1,
        "offset": 99999,
        "total": 100005,
        "next_offset": 100000,
    }
    page = service.search_players(limit=1, offset=100000)
    assert [row["playerId"] for row in page["data"]] == ["p100000"]
    assert page["pagination"] == {
        "limit": 1,
        "offset": 100000,
        "total": 100005,
        "next_offset": None,
    }
    assert service.search_players(limit=2, offset=99999)["pagination"]["next_offset"] is None


def test_complete_repository_seed_pagination(service, seed_player_records):
    assert list(seed_player_records[0]) == service.columns
    expected_ids = sorted(record["playerId"] for record in seed_player_records)
    assert len(expected_ids) == len(set(expected_ids)) == 19370

    default_page = service.search_players()
    assert len(default_page["data"]) == 20
    assert default_page["pagination"] == {
        "limit": 20,
        "offset": 0,
        "total": 19370,
        "next_offset": 20,
    }
    actual_ids = []
    offset = 0
    while True:
        page = service.search_players(limit=100, offset=offset)
        next_offset = offset + 100 if offset + 100 < len(expected_ids) else None
        assert page["pagination"] == {
            "limit": 100,
            "offset": offset,
            "total": 19370,
            "next_offset": next_offset,
        }
        assert len(page["data"]) == min(100, len(expected_ids) - offset)
        actual_ids.extend(row["playerId"] for row in page["data"])
        if next_offset is None:
            break
        offset = next_offset

    assert actual_ids == expected_ids
