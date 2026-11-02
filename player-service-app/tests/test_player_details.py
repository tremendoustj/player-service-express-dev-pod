import sqlite3
from contextlib import closing

import pytest

from player_service import PlayersNotFoundError, PlayerService


@pytest.fixture(autouse=True)
def verify_connections_closed(tracked_connections):
    yield
    for conn in tracked_connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            sqlite3.Connection.execute(conn, "SELECT 1")


@pytest.fixture
def service(player_db):
    return PlayerService(player_db)


@pytest.mark.parametrize("record_index", [0, 1, 3])
def test_detail_preserves_full_records_and_nulls(service, player_records, record_index):
    record = player_records[record_index]

    assert service.get_player(record["playerId"]) == record
    assert service.search_by_player(record["playerId"]) == record


@pytest.mark.parametrize("player_id", ["a", "A", "9", "Ab09" * 8])
def test_exact_id_length_and_character_boundaries(service, player_db, player_id):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("INSERT INTO players (playerId) VALUES (?)", (player_id,))
        conn.commit()

    result = service.get_player(player_id)

    assert result["playerId"] == player_id
    assert len(result) == 24
    assert all(value is None for key, value in result.items() if key != "playerId")


def test_details_use_binary_identity_even_with_nocase_storage(player_db):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DROP TABLE players")
        conn.execute("CREATE TABLE players (playerId TEXT COLLATE NOCASE)")
        conn.executemany("INSERT INTO players VALUES (?)", [("a",), ("A",)])
        conn.commit()
    service = PlayerService(player_db)

    assert service.get_player("a") == {"playerId": "a"}
    assert service.get_player("A") == {"playerId": "A"}


@pytest.mark.parametrize(
    "player_id",
    [
        None,
        1,
        1.0,
        True,
        b"aaronha01",
        [],
        {},
        "",
        " ",
        "a" * 33,
        " aaronha01",
        "aaronha01 ",
        "aaronha01\n",
        "a\tb",
        "a\x00b",
        "a_b",
        "a-b",
        "a/b",
        "quote'01",
        "\u00e9",
        "\uff21",
        "\u0661",
        "aaronha01' OR 1=1 --",
        "a'; DROP TABLE players; --",
    ],
)
def test_invalid_detail_ids_fail_before_querying(service, monkeypatch, player_id):
    def unexpected_connection():
        pytest.fail("Invalid detail IDs must not open a database connection")

    monkeypatch.setattr(service, "_read_connection", unexpected_connection)

    with pytest.raises(ValueError, match="1-32 ASCII alphanumeric characters"):
        service.get_player(player_id)


@pytest.mark.parametrize("player_id", ["absent", "Aaronha01", "aaron"])
def test_missing_detail_raises_explicit_error(service, player_id):
    with pytest.raises(PlayersNotFoundError) as caught:
        service.get_player(player_id)

    assert caught.value.missing_ids == (player_id,)
    assert str(caught.value) == "One or more requested player IDs were not found"


def test_empty_population_is_not_a_successful_empty_detail(service, player_db):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.commit()

    with pytest.raises(PlayersNotFoundError):
        service.get_player("aaronha01")


@pytest.mark.parametrize("duplicate_count", [0, 1, 100])
def test_detail_has_one_bounded_parameterized_query(
    service, player_db, player_records, monkeypatch, duplicate_count
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.executemany(
            "INSERT INTO players (playerId) VALUES (?)",
            [("aaronha01",)] * duplicate_count,
        )
        conn.commit()
    queries = []
    fetchall = service._fetchall

    def track_query(sql, parameters=()):
        rows = fetchall(sql, parameters)
        queries.append((sql, parameters, len(rows)))
        return rows

    monkeypatch.setattr(service, "_fetchall", track_query)
    if duplicate_count:
        with pytest.raises(sqlite3.DataError, match="unique stored player IDs"):
            service.get_player("aaronha01")
    else:
        assert service.get_player("aaronha01") == player_records[0]

    assert queries == [
        (
            "SELECT * FROM players WHERE playerId COLLATE BINARY = ? LIMIT ?",
            ("aaronha01", 2),
            2 if duplicate_count else 1,
        )
    ]


def test_unselected_duplicate_or_invalid_records_do_not_block_detail(
    service, player_db, player_records
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            "INSERT INTO players (playerId, height) VALUES (?, ?)",
            ("unknown01", float("inf")),
        )
        conn.commit()

    assert service.get_player("aaronha01") == player_records[0]


def test_legacy_lookup_still_accepts_ids_outside_the_integration_contract(
    service, player_records
):
    assert service.search_by_player("quote'01") == player_records[2]
    with pytest.raises(ValueError):
        service.get_player("quote'01")
