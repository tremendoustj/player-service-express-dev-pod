import sqlite3
from contextlib import closing
from pathlib import Path

from app_paths import PLAYER_DB


class PlayerService:
    def __init__(self, db_path=None):
        self.db_path = Path(PLAYER_DB if db_path is None else db_path).resolve()
        self.columns = self.get_columns()

    def _fetchall(self, query, parameters=()):
        with closing(
            sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True)
        ) as conn:
            conn.execute("PRAGMA query_only = ON")
            return conn.execute(query, parameters).fetchall()

    def get_all_players(self):
        result = self._fetchall("SELECT * FROM players")
        return [self.convert_row_to_dict(row) for row in result]

    def search_by_player(self, player_id):
        result = self._fetchall(
            "SELECT * FROM players WHERE playerId = ?", (player_id,)
        )
        return self.convert_row_to_dict(result[-1]) if result else {}

    def search_by_country(self, birth_country):
        return self._fetchall(
            "SELECT * FROM players WHERE birthCountry = ?", (birth_country,)
        )

    def convert_row_to_dict(self, row):
        dic = {self.columns[i]: row[i] for i in range(len(row))}
        return dic

    def get_columns(self):
        columns = [column[1] for column in self._fetchall("PRAGMA table_info(players)")]
        return columns
