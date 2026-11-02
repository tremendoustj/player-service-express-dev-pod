import sqlite3
import time
from contextlib import closing

import pytest

import player_service
from player_service import (
    MAX_DB_TIMEOUT_SECONDS,
    MIN_QUERY_TIMEOUT_SECONDS,
    PlayerFilters,
    PlayerQueryTimeoutError,
    PlayerService,
)


@pytest.fixture(autouse=True)
def verify_connections_closed(tracked_connections):
    yield
    for conn in tracked_connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            sqlite3.Connection.execute(conn, "SELECT 1")


@pytest.fixture
def clock(monkeypatch):
    current = [0.0]
    monkeypatch.setattr(player_service, "monotonic", lambda: current[0])
    return current


@pytest.mark.parametrize(
    "value",
    [True, False, "1", [], {}, float("nan"), float("inf"), -float("inf"), -1, 61, 10**400],
)
@pytest.mark.parametrize("name", ["busy_timeout_seconds", "query_timeout_seconds"])
def test_invalid_timeout_is_rejected_before_connecting(
    player_db, tracked_connections, name, value
):
    with pytest.raises(ValueError, match=name):
        PlayerService(player_db, **{name: value})

    assert tracked_connections == []


@pytest.mark.parametrize("value", [0, -0.0, MIN_QUERY_TIMEOUT_SECONDS / 2])
def test_query_timeout_must_be_at_least_one_millisecond(
    player_db, tracked_connections, value
):
    with pytest.raises(ValueError, match="query_timeout_seconds"):
        PlayerService(player_db, query_timeout_seconds=value)

    assert tracked_connections == []


def test_busy_timeout_cannot_be_none(player_db, tracked_connections):
    with pytest.raises(ValueError, match="busy_timeout_seconds"):
        PlayerService(player_db, busy_timeout_seconds=None)

    assert tracked_connections == []


@pytest.mark.parametrize("busy_timeout", [0, 0.001, 1, MAX_DB_TIMEOUT_SECONDS])
@pytest.mark.parametrize("query_timeout", [None, MIN_QUERY_TIMEOUT_SECONDS, 2, 60])
def test_timeout_boundaries_are_accepted(
    player_db, clock, busy_timeout, query_timeout
):
    service = PlayerService(
        player_db,
        busy_timeout_seconds=busy_timeout,
        query_timeout_seconds=query_timeout,
    )

    assert service.search_players()["pagination"]["total"] == 4
    expected_wait = busy_timeout
    if query_timeout is not None:
        expected_wait = min(busy_timeout, query_timeout)
    assert service._fetchall("PRAGMA busy_timeout") == [(int(expected_wait * 1000),)]


@pytest.mark.parametrize(
    ("method", "args", "kwargs"),
    [
        ("get_columns", (), {}),
        ("get_all_players", (), {}),
        ("search_by_player", ("aaronha01",), {}),
        ("search_by_player", ("missing",), {}),
        ("get_player", ("aaronha01",), {}),
        ("search_by_country", ("C\u00f4te d'Ivoire",), {}),
        ("search_players", (), {}),
        (
            "search_players",
            (PlayerFilters(q="Aaron", birth_country="USA"),),
            {"limit": 1, "offset": 1},
        ),
        ("get_player_statistics", (), {}),
        (
            "get_player_statistics",
            (PlayerFilters(throws="R"),),
            {"group_limit": 1},
        ),
        ("rank_players", ("height",), {"limit": 1, "offset": 1}),
        ("rank_players", ("birthYear",), {"direction": "asc"}),
        ("compare_players", (["aaronto01", "aaronha01"],), {}),
    ],
)
def test_deadlines_preserve_all_read_results(player_db, method, args, kwargs):
    legacy = PlayerService(player_db)
    bounded = PlayerService(
        player_db, busy_timeout_seconds=1, query_timeout_seconds=2
    )

    assert getattr(bounded, method)(*args, **kwargs) == getattr(legacy, method)(
        *args, **kwargs
    )


def test_legacy_defaults_do_not_install_a_progress_handler(
    player_db, monkeypatch, clock
):
    connect = sqlite3.connect

    class NoProgressConnection(sqlite3.Connection):
        def set_progress_handler(self, handler, steps):
            pytest.fail("Legacy reads must not install a query deadline")

    def connect_without_deadline(*args, **kwargs):
        assert kwargs["timeout"] == 5.0
        return connect(*args, factory=NoProgressConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_without_deadline)
    service = PlayerService(player_db)
    clock[0] += 1000

    assert service.query_timeout_seconds is None
    assert service._fetchall("PRAGMA busy_timeout") == [(5000,)]
    assert service.search_players()["pagination"]["total"] == 4


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
def test_progress_handler_is_removed_on_every_exit(
    player_db, monkeypatch, clock, outcome
):
    service = PlayerService(player_db, query_timeout_seconds=1)
    connect = sqlite3.connect
    updates = []

    class RecordingConnection(sqlite3.Connection):
        def set_progress_handler(self, handler, steps):
            updates.append((handler, steps))
            return super().set_progress_handler(handler, steps)

        def execute(self, query, parameters=()):
            result = super().execute(query, parameters)
            if outcome == "timeout" and query == "SELECT 1":
                clock[0] += 1
            return result

    def record_progress(*args, **kwargs):
        return connect(*args, factory=RecordingConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", record_progress)
    if outcome == "success":
        assert service._fetchall("SELECT 1") == [(1,)]
    elif outcome == "failure":
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            service._fetchall("SELECT * FROM missing_table")
    else:
        with pytest.raises(PlayerQueryTimeoutError):
            service._fetchall("SELECT 1")

    assert len(updates) == 2
    assert callable(updates[0][0])
    assert updates[0][1] == 1000
    assert updates[1] == (None, 0)


def test_deadline_includes_connection_setup(player_db, monkeypatch, clock):
    connect = sqlite3.connect
    statements = []

    def slow_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        clock[0] += 1
        return conn

    monkeypatch.setattr(sqlite3, "connect", slow_connect)
    with pytest.raises(PlayerQueryTimeoutError):
        PlayerService(player_db, query_timeout_seconds=1)

    assert statements == []


def test_schema_read_is_also_bounded(player_db, monkeypatch, clock):
    connect = sqlite3.connect

    class SlowSchemaConnection(sqlite3.Connection):
        def execute(self, query, parameters=()):
            result = super().execute(query, parameters)
            if query == "PRAGMA table_info(players)":
                clock[0] += 1
            return result

    def slow_schema(*args, **kwargs):
        return connect(*args, factory=SlowSchemaConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", slow_schema)
    with pytest.raises(PlayerQueryTimeoutError):
        PlayerService(player_db, query_timeout_seconds=1)


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("search_players", ()),
        ("rank_players", ("height",)),
        ("get_player_statistics", ()),
    ],
)
def test_statements_share_one_budget_and_shrink_the_lock_wait(
    player_db, monkeypatch, clock, method, args
):
    service = PlayerService(
        player_db, busy_timeout_seconds=5, query_timeout_seconds=1
    )
    connect = sqlite3.connect
    waits = []

    class SlowReadConnection(sqlite3.Connection):
        def execute(self, query, parameters=()):
            if query.startswith("SELECT "):
                waits.append(super().execute("PRAGMA busy_timeout").fetchone()[0])
            result = super().execute(query, parameters)
            if query.startswith("SELECT "):
                clock[0] += 0.75
            return result

    def slow_read(*args, **kwargs):
        return connect(*args, factory=SlowReadConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", slow_read)
    with pytest.raises(PlayerQueryTimeoutError):
        getattr(service, method)(*args)

    assert waits == [1000, 250]


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_columns", ()),
        ("get_all_players", ()),
        ("search_by_player", ("aaronha01",)),
        ("get_player", ("aaronha01",)),
        ("search_by_country", ("USA",)),
        ("search_players", ()),
        ("rank_players", ("height",)),
        ("get_player_statistics", ()),
        ("compare_players", (["aaronha01", "aaronto01"],)),
    ],
)
def test_short_reads_cannot_return_success_after_the_deadline(
    player_db, monkeypatch, clock, method, args
):
    service = PlayerService(player_db, query_timeout_seconds=1)
    connect = sqlite3.connect
    statements = []

    class ExpiringConnection(sqlite3.Connection):
        def execute(self, query, parameters=()):
            result = super().execute(query, parameters)
            if query.startswith("SELECT ") or query == "PRAGMA table_info(players)":
                statements.append(query)
                clock[0] += 1
            return result

    def expire_read(*args, **kwargs):
        return connect(*args, factory=ExpiringConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", expire_read)
    with pytest.raises(PlayerQueryTimeoutError):
        getattr(service, method)(*args)

    assert len(statements) == 1


def test_each_read_gets_a_fresh_budget(player_db, clock):
    service = PlayerService(player_db, query_timeout_seconds=1)

    for now in (10, 100, 1000):
        clock[0] = now
        assert service.search_players()["pagination"]["total"] == 4


def test_unrelated_storage_failure_is_not_relabelled_as_timeout(
    player_db, monkeypatch, clock
):
    service = PlayerService(player_db, query_timeout_seconds=1)
    connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, query, parameters=()):
            if query.startswith("SELECT "):
                clock[0] += 1
                raise sqlite3.OperationalError("simulated storage failure")
            return super().execute(query, parameters)

    def fail_read(*args, **kwargs):
        return connect(*args, factory=FailingConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fail_read)
    with pytest.raises(sqlite3.OperationalError, match="simulated storage failure"):
        service.search_players()


@pytest.mark.parametrize("selection", ["SUM(value)", "value"])
def test_real_cpu_bound_query_is_interrupted_and_subsequent_reads_work(
    player_db, selection
):
    service = PlayerService(player_db, query_timeout_seconds=0.05)

    started = time.monotonic()
    with pytest.raises(
        PlayerQueryTimeoutError, match="Player database read deadline exceeded"
    ) as failure:
        service._fetchall(
            "WITH RECURSIVE numbers(value) AS "
            "(SELECT 1 UNION ALL SELECT value + 1 FROM numbers WHERE value < 100000000) "
            f"SELECT {selection} FROM numbers"
        )
    elapsed = time.monotonic() - started

    assert 0.025 <= elapsed < 0.75
    assert isinstance(failure.value.__cause__, sqlite3.OperationalError)
    assert str(failure.value.__cause__) == "interrupted"
    assert service.search_players()["pagination"]["total"] == 4


def test_real_search_timeout_releases_its_read_transaction(player_db):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("ALTER TABLE players RENAME TO player_source")
        joins = " ".join(
            f"CROSS JOIN player_source AS extra{index}" for index in range(14)
        )
        conn.execute(
            f"CREATE VIEW players AS SELECT source.* FROM player_source AS source {joins}"
        )
        conn.commit()
    service = PlayerService(player_db, query_timeout_seconds=0.05)

    started = time.monotonic()
    with pytest.raises(PlayerQueryTimeoutError):
        service.search_players()
    assert time.monotonic() - started < 0.75

    with closing(sqlite3.connect(player_db, timeout=0)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("DROP VIEW players")
        writer.execute("ALTER TABLE player_source RENAME TO players")
        writer.commit()
    assert service.search_players()["pagination"]["total"] == 4


@pytest.mark.parametrize(
    ("busy_timeout", "query_timeout"),
    [(0.05, None), (0.05, 1), (1, 0.05), (0, 1)],
)
def test_real_locked_database_wait_is_bounded_and_remains_a_storage_error(
    player_db, busy_timeout, query_timeout
):
    service = PlayerService(
        player_db,
        busy_timeout_seconds=busy_timeout,
        query_timeout_seconds=query_timeout,
    )
    with closing(sqlite3.connect(player_db)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            service.search_players()
        elapsed = time.monotonic() - started
        writer.rollback()

    assert elapsed < 0.75
    if busy_timeout:
        assert elapsed >= 0.025
    assert service.search_players()["pagination"]["total"] == 4


def test_schema_lock_wait_is_bounded(player_db):
    with closing(sqlite3.connect(player_db)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            PlayerService(
                player_db, busy_timeout_seconds=1, query_timeout_seconds=0.05
            )
        assert time.monotonic() - started < 0.75
        writer.rollback()


def test_deadline_connections_still_enforce_read_only_access(player_db, player_records):
    service = PlayerService(player_db, query_timeout_seconds=2)

    assert service._fetchall("PRAGMA query_only") == [(1,)]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        service._fetchall("DELETE FROM players")
    assert service.get_all_players() == player_records


def test_bounded_repository_seed_reads_match_legacy(player_db, seed_player_records):
    legacy = PlayerService(player_db)
    bounded = PlayerService(
        player_db, busy_timeout_seconds=1, query_timeout_seconds=2
    )
    operations = [
        ("search_players", (), {"limit": 100}),
        ("search_by_player", ("aaronha01",), {}),
        ("get_player", ("aaronha01",), {}),
        ("get_player_statistics", (), {"group_limit": 100}),
        ("rank_players", ("height",), {"limit": 100}),
        ("compare_players", (["aaronha01", "aaronto01"],), {}),
    ]

    assert len(seed_player_records) == 19370
    for method, args, kwargs in operations:
        assert getattr(bounded, method)(*args, **kwargs) == getattr(legacy, method)(
            *args, **kwargs
        )
