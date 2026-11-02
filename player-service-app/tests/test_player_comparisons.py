import json
import sqlite3
from contextlib import closing

import pytest

from player_service import PlayersNotFoundError, PlayerService


@pytest.fixture
def service(player_db):
    return PlayerService(player_db)


@pytest.mark.parametrize("container", [list, tuple])
def test_comparison_preserves_full_records_in_requested_order(
    service, player_records, container
):
    result = service.compare_players(container(["aaronto01", "aaronha01"]))

    assert result == {
        "data": [player_records[1], player_records[0]],
        "differences": [
            {
                "left_player_id": "aaronto01",
                "right_player_id": "aaronha01",
                "numeric": {"birthYear": 5.0, "height": 3.0, "weight": 10.0},
            }
        ],
    }
    json.dumps(result, allow_nan=False)
    assert service.get_all_players() == player_records


def test_differences_cover_every_pair_in_request_order(service, player_records):
    assert service.compare_players(["unknown01", "aaronha01", "aaronto01"]) == {
        "data": [player_records[3], player_records[0], player_records[1]],
        "differences": [
            {
                "left_player_id": "unknown01",
                "right_player_id": "aaronha01",
                "numeric": {"birthYear": None, "height": None, "weight": None},
            },
            {
                "left_player_id": "unknown01",
                "right_player_id": "aaronto01",
                "numeric": {"birthYear": None, "height": None, "weight": None},
            },
            {
                "left_player_id": "aaronha01",
                "right_player_id": "aaronto01",
                "numeric": {"birthYear": -5.0, "height": -3.0, "weight": -10.0},
            },
        ],
    }


@pytest.mark.parametrize("count", [2, 3, 4, 5])
def test_exact_cardinality_and_id_boundaries_with_case_sensitive_ids(
    service, player_db, count
):
    player_ids = ["a", "A", "9", "Ab09" * 8, "z"][:count]
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.executemany(
            "INSERT INTO players (playerId) VALUES (?)",
            [(player_id,) for player_id in reversed(player_ids)],
        )
        conn.commit()

    result = service.compare_players(player_ids)

    assert set(result) == {"data", "differences"}
    assert [row["playerId"] for row in result["data"]] == player_ids
    assert len(result["differences"]) == count * (count - 1) // 2
    assert [
        (pair["left_player_id"], pair["right_player_id"])
        for pair in result["differences"]
    ] == [
        (player_ids[left], player_ids[right])
        for left in range(count)
        for right in range(left + 1, count)
    ]
    for pair in result["differences"]:
        assert pair["numeric"] == {"birthYear": None, "height": None, "weight": None}
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "player_ids",
    [None, "aaronha01,aaronto01", b"ids", 2, 2.0, True, {}, {"a", "b"}, iter(["a", "b"])],
)
def test_unordered_or_nonsequence_input_fails_before_querying(
    service, monkeypatch, player_ids
):
    def unexpected_connection():
        pytest.fail("Invalid comparison input must not open a database connection")

    monkeypatch.setattr(service, "_read_connection", unexpected_connection)

    with pytest.raises(TypeError, match="player_ids must be a list or tuple"):
        service.compare_players(player_ids)


@pytest.mark.parametrize("count", [0, 1, 6, 100])
def test_invalid_cardinality_fails_before_querying(service, monkeypatch, count):
    def unexpected_connection():
        pytest.fail("Invalid comparison sizes must not open a database connection")

    monkeypatch.setattr(service, "_read_connection", unexpected_connection)

    with pytest.raises(ValueError, match="player_ids must contain between 2 and 5 IDs"):
        service.compare_players([f"p{index}" for index in range(count)])


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
def test_invalid_id_fails_before_querying(service, monkeypatch, player_id):
    def unexpected_connection():
        pytest.fail("Invalid comparison IDs must not open a database connection")

    monkeypatch.setattr(service, "_read_connection", unexpected_connection)

    with pytest.raises(ValueError, match="1-32 ASCII alphanumeric characters"):
        service.compare_players(["aaronto01", player_id])


@pytest.mark.parametrize(
    "player_ids",
    [
        ["aaronha01", "aaronha01"],
        ["aaronha01", "aaronto01", "aaronha01"],
        ["missing", "missing"],
    ],
)
def test_duplicate_request_ids_fail_before_querying(service, monkeypatch, player_ids):
    def unexpected_connection():
        pytest.fail("Duplicate comparison IDs must not open a database connection")

    monkeypatch.setattr(service, "_read_connection", unexpected_connection)

    with pytest.raises(ValueError, match="player_ids must contain distinct IDs"):
        service.compare_players(player_ids)


@pytest.mark.parametrize(
    ("player_ids", "missing_ids"),
    [
        (["aaronha01", "absent"], ("absent",)),
        (["absent2", "aaronha01", "absent1"], ("absent2", "absent1")),
        (["absent2", "absent1"], ("absent2", "absent1")),
        (["Aaronha01", "aaronto01"], ("Aaronha01",)),
    ],
)
def test_missing_players_raise_an_explicit_ordered_error(
    service, player_records, player_ids, missing_ids
):
    with pytest.raises(PlayersNotFoundError) as error:
        service.compare_players(player_ids)

    assert error.value.missing_ids == missing_ids
    assert str(error.value) == "One or more requested player IDs were not found"
    assert service.get_all_players() == player_records


def test_empty_population_reports_every_requested_id_as_missing(service, player_db):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.commit()

    with pytest.raises(PlayersNotFoundError) as error:
        service.compare_players(["aaronto01", "aaronha01"])

    assert error.value.missing_ids == ("aaronto01", "aaronha01")


@pytest.mark.parametrize("field", ["birthYear", "height", "weight"])
@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (None, 2.5, None),
        (2.5, None, None),
        (None, None, None),
        (0, 0, 0.0),
        (0, 2.5, -2.5),
        (-1.5, 2.5, -4.0),
        (2.5, -1.5, 4.0),
        (1e308, 1e308, 0.0),
        (1e308, 0, 1e308),
    ],
)
def test_numeric_differences_preserve_nulls_zeros_and_finite_values(
    service, player_db, field, left, right, expected
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.executemany(
            f"UPDATE players SET {field} = ? WHERE playerId = ?",
            [(left, "aaronha01"), (right, "aaronto01")],
        )
        conn.commit()

    result = service.compare_players(["aaronha01", "aaronto01"])

    assert [row[field] for row in result["data"]] == [left, right]
    assert result["differences"][0]["numeric"][field] == expected
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("field", ["birthYear", "height", "weight"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), "not-numeric", b"invalid"])
@pytest.mark.parametrize("other_value", [None, 1.0])
def test_invalid_selected_values_raise_even_with_a_null_counterpart(
    service, player_db, field, value, other_value
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.executemany(
            f"UPDATE players SET {field} = ? WHERE playerId = ?",
            [(other_value, "aaronha01"), (value, "aaronto01")],
        )
        conn.commit()

    with pytest.raises(
        sqlite3.DataError, match=f"{field} comparisons require finite numeric values"
    ):
        service.compare_players(["aaronha01", "aaronto01"])


@pytest.mark.parametrize("field", ["birthYear", "height", "weight"])
@pytest.mark.parametrize("left", [1e308, -1e308])
def test_overflowing_differences_raise_despite_finite_operands(
    service, player_db, field, left
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.executemany(
            f"UPDATE players SET {field} = ? WHERE playerId = ?",
            [(left, "aaronha01"), (-left, "aaronto01")],
        )
        conn.commit()

    with pytest.raises(
        sqlite3.DataError, match=f"{field} comparisons require finite numeric differences"
    ):
        service.compare_players(["aaronha01", "aaronto01"])


def test_invalid_values_and_duplicate_ids_outside_selection_do_not_block_comparisons(
    service, player_db, player_records
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            "UPDATE players SET height = ? WHERE playerId = ?", ("invalid", "unknown01")
        )
        conn.execute(
            "INSERT INTO players SELECT * FROM players WHERE playerId = ?", ("unknown01",)
        )
        conn.commit()

    result = service.compare_players(["aaronha01", "aaronto01"])

    assert result["data"] == player_records[:2]
    assert result["differences"][0]["numeric"] == {
        "birthYear": -5.0,
        "height": -3.0,
        "weight": -10.0,
    }


@pytest.mark.parametrize("other_id", ["aaronto01", "missing"])
def test_duplicate_stored_ids_raise_instead_of_choosing_an_arbitrary_record(
    service, player_db, other_id
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            "INSERT INTO players SELECT * FROM players WHERE playerId = ?", ("aaronha01",)
        )
        conn.commit()

    with pytest.raises(
        sqlite3.DataError, match="Comparisons require unique stored player IDs"
    ):
        service.compare_players(["aaronha01", other_id])


@pytest.mark.parametrize("count", [2, 5])
def test_duplicate_detection_has_a_bounded_parameterized_fetch(
    service, player_db, monkeypatch, count
):
    player_ids = [f"p{index}" for index in range(count)]
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.executemany(
            "INSERT INTO players (playerId) VALUES (?)",
            [(player_id,) for player_id in player_ids] + [(player_ids[-1],)] * 100,
        )
        conn.commit()
    queries = []
    fetchall = service._fetchall

    def track_query(sql, parameters=()):
        rows = fetchall(sql, parameters)
        queries.append((sql, parameters, len(rows)))
        return rows

    monkeypatch.setattr(service, "_fetchall", track_query)

    with pytest.raises(sqlite3.DataError, match="unique stored player IDs"):
        service.compare_players(player_ids)

    assert len(queries) == 1
    sql, parameters, row_count = queries[0]
    assert sql.endswith("LIMIT ?")
    assert all(player_id not in sql for player_id in player_ids)
    assert parameters == (*player_ids, count + 1)
    assert row_count == count + 1


def test_all_players_and_differences_share_one_query_snapshot(
    service, player_db, player_records, monkeypatch
):
    with closing(sqlite3.connect(player_db)) as conn:
        assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    connect = sqlite3.connect
    queries = []

    class ConcurrentWriteConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if sql.startswith("SELECT * FROM players"):
                queries.append(sql)
                with closing(connect(player_db)) as writer:
                    writer.execute("UPDATE players SET height = 100")
                    writer.commit()
            return cursor

    def connect_with_writer(*args, **kwargs):
        return connect(*args, factory=ConcurrentWriteConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_with_writer)

    result = service.compare_players(["aaronto01", "aaronha01"])

    assert result["data"] == [player_records[1], player_records[0]]
    assert result["differences"][0]["numeric"]["height"] == 3.0
    assert len(queries) == 1
    with closing(connect(player_db)) as conn:
        heights = conn.execute("SELECT DISTINCT height FROM players").fetchall()
    assert heights == [(100.0,)]


def test_repository_seed_comparisons_match_independent_records_and_differences(
    service, seed_player_records
):
    assert len(seed_player_records) == 19370
    assert all(
        row["playerId"].isascii()
        and row["playerId"].isalnum()
        and 1 <= len(row["playerId"]) <= 32
        for row in seed_player_records
    )
    selected = [seed_player_records[-1], seed_player_records[0]]
    for field in ("birthYear", "height", "weight"):
        selected.append(
            next(
                row
                for row in seed_player_records
                if row[field] is None
                and row["playerId"] not in {record["playerId"] for record in selected}
            )
        )
    numeric_columns = (
        "birthYear",
        "birthMonth",
        "birthDay",
        "deathYear",
        "deathMonth",
        "deathDay",
        "height",
        "weight",
    )
    expected_records = [
        {
            column: (
                float(value) if column in numeric_columns and value is not None else value
            )
            for column, value in row.items()
        }
        for row in selected
    ]
    expected_differences = []
    for left_index, left in enumerate(selected):
        for right in selected[left_index + 1 :]:
            expected_differences.append(
                {
                    "left_player_id": left["playerId"],
                    "right_player_id": right["playerId"],
                    "numeric": {
                        field: (
                            None
                            if left[field] is None or right[field] is None
                            else float(left[field]) - float(right[field])
                        )
                        for field in ("birthYear", "height", "weight")
                    },
                }
            )

    result = service.compare_players([row["playerId"] for row in selected])

    assert result == {"data": expected_records, "differences": expected_differences}
    assert len(result["data"]) == 5
    assert len(result["differences"]) == 10
    json.dumps(result, allow_nan=False)
