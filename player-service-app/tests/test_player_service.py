import sqlite3
from contextlib import closing

import pytest

from player_service import PlayersNotFoundError, PlayerService


def assert_connections_closed(connections):
    assert connections
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


def test_get_all_players_preserves_fields_and_values(player_db, player_records):
    service = PlayerService(player_db)

    assert service.get_all_players() == player_records
    assert service.get_columns() == list(player_records[0])
    assert (
        service.convert_row_to_dict(tuple(player_records[0].values()))
        == player_records[0]
    )


@pytest.mark.parametrize("record_index", [0, 1, 2, 3])
def test_search_by_player_returns_exact_record(player_db, player_records, record_index):
    record = player_records[record_index]
    assert PlayerService(player_db).search_by_player(record["playerId"]) == record


@pytest.mark.parametrize(
    "player_id",
    ["missing", "aaronha01' OR 1=1 --", "aaronha01'; DROP TABLE players; --"],
)
def test_unmatched_player_input_does_not_change_query(player_db, player_records, player_id):
    service = PlayerService(player_db)

    assert service.search_by_player(player_id) == {}
    assert service.get_all_players() == player_records


@pytest.mark.parametrize(
    ("country", "record_indexes"),
    [
        ("USA", [0, 1]),
        ("C\u00f4te d'Ivoire", [2]),
        ("missing", []),
        ("USA' OR 1=1 --", []),
        ("USA'; DROP TABLE players; --", []),
    ],
)
def test_search_by_country_preserves_raw_rows(
    player_db, player_records, country, record_indexes
):
    service = PlayerService(player_db)
    expected = [tuple(player_records[index].values()) for index in record_indexes]

    assert service.search_by_country(country) == expected
    assert service.get_all_players() == player_records


def test_empty_population_returns_empty_results(player_db):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.commit()
    service = PlayerService(player_db)

    assert service.get_all_players() == []
    assert service.search_by_player("aaronha01") == {}
    assert service.search_by_country("USA") == []


def test_all_reads_close_connections(player_db, tracked_connections):
    service = PlayerService(player_db)
    assert_connections_closed(tracked_connections)

    service.get_all_players()
    service.search_by_player("aaronha01")
    service.search_by_player("missing")
    service.search_by_country("USA")
    service.search_by_country("missing")
    service.search_players()
    service.search_players(offset=100000)
    service.rank_players("height")
    service.rank_players("birthYear", limit=1, offset=100000)
    service.get_player_statistics()
    service.get_player_statistics(group_limit=1)
    service.compare_players(["aaronha01", "aaronto01"])
    with pytest.raises(PlayersNotFoundError):
        service.compare_players(["aaronha01", "missing"])
    service.get_columns()

    assert_connections_closed(tracked_connections)


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_all_players", ()),
        ("search_by_player", ("aaronha01",)),
        ("search_by_country", ("USA",)),
        ("search_players", ()),
        ("rank_players", ("height",)),
        ("get_player_statistics", ()),
        ("compare_players", (["aaronha01", "aaronto01"],)),
    ],
)
def test_failed_queries_raise_and_close_connections(
    player_db, tracked_connections, method, args
):
    service = PlayerService(player_db)
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DROP TABLE players")
        conn.commit()

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        getattr(service, method)(*args)

    assert_connections_closed(tracked_connections)


@pytest.mark.parametrize(
    ("method", "args"),
    [("search_players", ()), ("rank_players", ("height",))],
)
def test_failed_page_closes_transaction(
    player_db, tracked_connections, monkeypatch, method, args
):
    service = PlayerService(player_db)
    connect = sqlite3.connect

    class FailingPageConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith("SELECT * FROM players"):
                raise sqlite3.OperationalError("simulated page failure")
            return super().execute(sql, parameters)

    def fail_page(*args, **kwargs):
        return connect(*args, factory=FailingPageConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fail_page)

    with pytest.raises(sqlite3.OperationalError, match="simulated page failure"):
        getattr(service, method)(*args)

    assert_connections_closed(tracked_connections)


@pytest.mark.parametrize("field", ["birthCountry", "bats", "throws"])
def test_failed_statistics_group_closes_transaction(
    player_db, tracked_connections, monkeypatch, field
):
    service = PlayerService(player_db)
    connect = sqlite3.connect

    class FailingGroupConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith(f"SELECT {field} AS value"):
                raise sqlite3.OperationalError("simulated group failure")
            return super().execute(sql, parameters)

    def fail_group(*args, **kwargs):
        return connect(*args, factory=FailingGroupConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fail_group)

    with pytest.raises(sqlite3.OperationalError, match="simulated group failure"):
        service.get_player_statistics()

    assert_connections_closed(tracked_connections)


@pytest.mark.parametrize(
    ("method", "args", "message"),
    [
        ("get_player_statistics", (), "height statistics"),
        ("rank_players", ("height",), "height rankings"),
        ("compare_players", (["aaronha01", "aaronto01"],), "height comparisons"),
    ],
)
def test_invalid_numeric_data_closes_transaction(
    player_db, tracked_connections, method, args, message
):
    service = PlayerService(player_db)
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("UPDATE players SET height = ?", (float("inf"),))
        conn.commit()

    with pytest.raises(sqlite3.DataError, match=message):
        getattr(service, method)(*args)

    assert_connections_closed(tracked_connections)


@pytest.mark.parametrize("failure", ["duplicate", "overflow"])
def test_comparison_data_errors_close_connections(
    player_db, tracked_connections, failure
):
    service = PlayerService(player_db)
    with closing(sqlite3.connect(player_db)) as conn:
        if failure == "duplicate":
            conn.execute(
                "INSERT INTO players SELECT * FROM players WHERE playerId = ?",
                ("aaronha01",),
            )
            message = "unique stored player IDs"
        else:
            conn.executemany(
                "UPDATE players SET height = ? WHERE playerId = ?",
                [(1e308, "aaronha01"), (-1e308, "aaronto01")],
            )
            message = "finite numeric differences"
        conn.commit()

    with pytest.raises(sqlite3.DataError, match=message):
        service.compare_players(["aaronha01", "aaronto01"])

    assert_connections_closed(tracked_connections)


def test_invalid_database_raises_and_closes_connection(tmp_path, tracked_connections):
    db_path = tmp_path / "invalid.db"
    db_path.write_text("not a SQLite database")

    with pytest.raises(sqlite3.DatabaseError, match="not a database"):
        PlayerService(db_path)

    assert_connections_closed(tracked_connections)


def test_missing_database_is_not_created(tmp_path):
    db_path = tmp_path / "missing.db"

    with pytest.raises(sqlite3.OperationalError, match="unable to open database"):
        PlayerService(db_path)

    assert not db_path.exists()


def test_database_paths_escape_uri_characters(player_db, player_records):
    db_path = player_db.with_name("players #? \u00e9.db")
    player_db.rename(db_path)

    assert PlayerService(db_path).get_all_players() == player_records


def test_connections_are_query_only_and_reject_writes(
    player_db, player_records, tracked_connections
):
    service = PlayerService(player_db)

    assert service._fetchall("PRAGMA query_only") == [(1,)]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        service._fetchall("DELETE FROM players")

    assert service.get_all_players() == player_records
    assert_connections_closed(tracked_connections)
