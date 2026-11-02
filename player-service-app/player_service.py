import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Iterator, Optional

from app_paths import PLAYER_DB


DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100
MAX_PAGE_OFFSET = 100000
MAX_FILTER_TEXT_LENGTH = 100
DEFAULT_GROUP_LIMIT = 20
MAX_GROUP_LIMIT = 100


def _validate_integer(name: str, value: int, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


@dataclass(frozen=True)
class PlayerFilters:
    q: Optional[str] = None
    birth_country: Optional[str] = None
    birth_year_min: Optional[int] = None
    birth_year_max: Optional[int] = None
    bats: Optional[str] = None
    throws: Optional[str] = None

    def __post_init__(self) -> None:
        for name, value in (("q", self.q), ("birth_country", self.birth_country)):
            if value is not None and (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > MAX_FILTER_TEXT_LENGTH
                or not value.isprintable()
            ):
                raise ValueError(
                    f"{name} must be 1-{MAX_FILTER_TEXT_LENGTH} printable characters"
                )
        for name, value in (
            ("birth_year_min", self.birth_year_min),
            ("birth_year_max", self.birth_year_max),
        ):
            if value is not None:
                _validate_integer(name, value, 1, 9999)
        if (
            self.birth_year_min is not None
            and self.birth_year_max is not None
            and self.birth_year_min > self.birth_year_max
        ):
            raise ValueError("birth_year_min must not exceed birth_year_max")
        for name, value, choices in (
            ("bats", self.bats, ("L", "R", "B")),
            ("throws", self.throws, ("L", "R", "S")),
        ):
            if value is not None and (
                not isinstance(value, str) or value not in choices
            ):
                raise ValueError(f"{name} must be one of {', '.join(choices)}")


class PlayerService:
    def __init__(self, db_path=None):
        self.db_path = Path(PLAYER_DB if db_path is None else db_path).resolve()
        self.columns = self.get_columns()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        with closing(
            sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True)
        ) as conn:
            conn.execute("PRAGMA query_only = ON")
            yield conn

    def _fetchall(self, query, parameters=()):
        with self._read_connection() as conn:
            return conn.execute(query, parameters).fetchall()

    @staticmethod
    def _filter_clause(filters: Optional[PlayerFilters]) -> tuple:
        if filters is None:
            filters = PlayerFilters()
        elif not isinstance(filters, PlayerFilters):
            raise TypeError("filters must be a PlayerFilters instance")
        conditions = []
        parameters = []
        if filters.q is not None:
            literal = (
                filters.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            search_fields = (
                "playerId",
                "nameFirst",
                "nameLast",
                "nameGiven",
                "(COALESCE(nameFirst, '') || ' ' || COALESCE(nameLast, ''))",
            )
            conditions.append(
                "("
                + " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for field in search_fields)
                + ")"
            )
            parameters.extend([f"%{literal}%"] * len(search_fields))
        for column, operator, value in (
            ("birthCountry COLLATE BINARY", "=", filters.birth_country),
            ("birthYear", ">=", filters.birth_year_min),
            ("birthYear", "<=", filters.birth_year_max),
            ("bats", "=", filters.bats),
            ("throws", "=", filters.throws),
        ):
            if value is not None:
                conditions.append(f"{column} {operator} ?")
                parameters.append(value)
        clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        return clause, tuple(parameters)

    def search_players(
        self,
        filters: Optional[PlayerFilters] = None,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> dict:
        where_clause, parameters = self._filter_clause(filters)
        _validate_integer("limit", limit, 1, MAX_PAGE_LIMIT)
        _validate_integer("offset", offset, 0, MAX_PAGE_OFFSET)
        with self._read_connection() as conn:
            # COUNT and the page must observe the same SQLite snapshot.
            conn.execute("BEGIN")
            total = conn.execute(
                f"SELECT COUNT(*) FROM players{where_clause}", parameters
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM players{where_clause} "
                "ORDER BY playerId COLLATE BINARY ASC LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
        next_offset = offset + limit
        return {
            "data": [self.convert_row_to_dict(row) for row in rows],
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total,
                "next_offset": (
                    next_offset
                    if next_offset < total and next_offset <= MAX_PAGE_OFFSET
                    else None
                ),
            },
        }

    def get_player_statistics(
        self,
        filters: Optional[PlayerFilters] = None,
        *,
        group_limit: int = DEFAULT_GROUP_LIMIT,
    ) -> dict:
        where_clause, parameters = self._filter_clause(filters)
        _validate_integer("group_limit", group_limit, 1, MAX_GROUP_LIMIT)
        numeric_fields = ("birthYear", "height", "weight")
        group_fields = ("birthCountry", "bats", "throws")
        aggregates = ["COUNT(*) AS total"]
        for field in numeric_fields:
            aggregates.extend(
                f"{function}({field}) AS {field}_{name}"
                for function, name in (
                    ("COUNT", "known_count"),
                    ("MIN", "min"),
                    ("MAX", "max"),
                    ("AVG", "mean"),
                )
            )
        for field in group_fields:
            aggregates.extend(
                (
                    f"COUNT({field}) AS {field}_known_count",
                    f"COUNT(DISTINCT {field} COLLATE BINARY) AS {field}_total_groups",
                )
            )

        with self._read_connection() as conn:
            conn.row_factory = sqlite3.Row
            # Numeric summaries and all groups must describe the same population.
            conn.execute("BEGIN")
            summary = conn.execute(
                f"SELECT {', '.join(aggregates)} FROM players{where_clause}", parameters
            ).fetchone()
            total = summary["total"]
            numeric = {}
            for field in numeric_fields:
                known_count = summary[f"{field}_known_count"]
                measures = {
                    name: summary[f"{field}_{name}"] for name in ("min", "max", "mean")
                }
                if known_count and any(
                    not isinstance(value, (int, float)) or not isfinite(value)
                    for value in measures.values()
                ):
                    raise sqlite3.DataError(
                        f"{field} statistics require finite numeric values"
                    )
                numeric[field] = {
                    "known_count": known_count,
                    "missing_count": total - known_count,
                    **measures,
                }
            groups = {}
            for field in group_fields:
                group_where = (
                    f"{where_clause} AND {field} IS NOT NULL"
                    if where_clause
                    else f" WHERE {field} IS NOT NULL"
                )
                rows = conn.execute(
                    f"SELECT {field} AS value, COUNT(*) AS count FROM players{group_where} "
                    f"GROUP BY {field} COLLATE BINARY "
                    f"ORDER BY count DESC, {field} COLLATE BINARY ASC LIMIT ?",
                    (*parameters, group_limit),
                ).fetchall()
                known_count = summary[f"{field}_known_count"]
                groups[field] = {
                    "data": [dict(row) for row in rows],
                    "known_count": known_count,
                    "missing_count": total - known_count,
                    "total_groups": summary[f"{field}_total_groups"],
                    "limit": group_limit,
                    "other_count": known_count - sum(row["count"] for row in rows),
                }

        return {"total": total, "numeric": numeric, "groups": groups}

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
