import csv
import sqlite3
from contextlib import closing

import pytest

from app_paths import PLAYER_CSV


PLAYER_COLUMNS = (
    "playerId",
    "birthYear",
    "birthMonth",
    "birthDay",
    "birthCountry",
    "birthState",
    "birthCity",
    "deathYear",
    "deathMonth",
    "deathDay",
    "deathCountry",
    "deathState",
    "deathCity",
    "nameFirst",
    "nameLast",
    "nameGiven",
    "weight",
    "height",
    "bats",
    "throws",
    "debut",
    "finalGame",
    "retroID",
    "bbrefID",
)


@pytest.fixture
def player_records():
    records = [
        {
            "playerId": "aaronha01",
            "birthYear": 1934.0,
            "birthMonth": 2.0,
            "birthDay": 5.0,
            "birthCountry": "USA",
            "birthState": "AL",
            "birthCity": "Mobile",
            "nameFirst": "Hank",
            "nameLast": "Aaron",
            "nameGiven": "Henry Louis",
            "weight": 180.0,
            "height": 72.0,
            "bats": "R",
            "throws": "R",
            "debut": "1954-04-13",
            "finalGame": "1976-10-03",
            "retroID": "aaroh101",
            "bbrefID": "aaronha01",
        },
        {
            "playerId": "aaronto01",
            "birthYear": 1939.0,
            "birthCountry": "USA",
            "nameFirst": "Tommie",
            "nameLast": "Aaron",
            "weight": 190.0,
            "height": 75.0,
            "bats": "R",
            "throws": "R",
        },
        {
            "playerId": "quote'01",
            "birthCountry": "C\u00f4te d'Ivoire",
            "nameFirst": "Ren\u00e9",
            "nameLast": "O'Neil",
            "bats": "B",
            "throws": "S",
        },
        {"playerId": "unknown01"},
    ]
    return [
        {column: record.get(column) for column in PLAYER_COLUMNS} for record in records
    ]


@pytest.fixture
def player_db(tmp_path, player_records):
    db_path = tmp_path / "players.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE players (
                playerId TEXT,
                birthYear FLOAT, birthMonth FLOAT, birthDay FLOAT,
                birthCountry TEXT, birthState TEXT, birthCity TEXT,
                deathYear FLOAT, deathMonth FLOAT, deathDay FLOAT,
                deathCountry TEXT, deathState TEXT, deathCity TEXT,
                nameFirst TEXT, nameLast TEXT, nameGiven TEXT,
                weight FLOAT, height FLOAT, bats TEXT, throws TEXT,
                debut TEXT, finalGame TEXT, retroID TEXT, bbrefID TEXT
            )
            """
        )
        placeholders = ", ".join("?" for _ in PLAYER_COLUMNS)
        conn.executemany(
            f"INSERT INTO players VALUES ({placeholders})",
            [
                tuple(record[column] for column in PLAYER_COLUMNS)
                for record in player_records
            ],
        )
        conn.commit()
    return db_path


@pytest.fixture
def tracked_connections(monkeypatch, player_db):
    connections = []
    connect = sqlite3.connect

    def track_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", track_connect)
    return connections


@pytest.fixture
def seed_player_records(player_db):
    with PLAYER_CSV.open(encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        assert reader.fieldnames == list(PLAYER_COLUMNS)
        records = [
            {column: value or None for column, value in record.items()}
            for record in reader
        ]
    placeholders = ", ".join("?" for _ in PLAYER_COLUMNS)
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute("DELETE FROM players")
        conn.executemany(
            f"INSERT INTO players VALUES ({placeholders})",
            [tuple(record[column] for column in PLAYER_COLUMNS) for record in records],
        )
        conn.commit()
    return records


@pytest.fixture
def client(player_db, monkeypatch):
    import app_paths
    import player_service

    # App import seeds a database, so redirect its path before importing it.
    monkeypatch.setattr(app_paths, "PLAYER_DB", player_db)
    monkeypatch.setattr(player_service, "PLAYER_DB", player_db)
    import app as app_module

    monkeypatch.setattr(app_module, "PLAYER_DB", player_db)
    monkeypatch.setitem(app_module.app.config, "TESTING", True)
    with app_module.app.test_client() as client:
        yield client
