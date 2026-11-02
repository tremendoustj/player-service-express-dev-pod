import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from itertools import combinations
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import Iterator, Optional, Union

from app_paths import PLAYER_DB


DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100
MAX_PAGE_OFFSET = 100000
MAX_FILTER_TEXT_LENGTH = 100
DEFAULT_GROUP_LIMIT = 20
MAX_GROUP_LIMIT = 100
MAX_COMPARISON_PLAYERS = 5
MAX_PLAYER_ID_LENGTH = 32
DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0
MIN_QUERY_TIMEOUT_SECONDS = 0.001
MAX_DB_TIMEOUT_SECONDS = 60.0
_PROGRESS_HANDLER_STEPS = 1000


def validate_integer(name: str, value: int, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


def validate_player_id(player_id: str) -> None:
    if (
        not isinstance(player_id, str)
        or not 1 <= len(player_id) <= MAX_PLAYER_ID_LENGTH
        or not player_id.isascii()
        or not player_id.isalnum()
    ):
        raise ValueError(
            f"player IDs must be 1-{MAX_PLAYER_ID_LENGTH} ASCII alphanumeric characters"
        )


def validate_db_timeout(name: str, value: float, minimum: float) -> None:
    if type(value) not in (int, float) or not minimum <= value <= MAX_DB_TIMEOUT_SECONDS:
        raise ValueError(
            f"{name} must be a finite number between "
            f"{minimum:g} and {MAX_DB_TIMEOUT_SECONDS:g} seconds"
        )


def _pagination(limit: int, offset: int, total: int) -> dict:
    next_offset = offset + limit
    return {
        "limit": limit,
        "offset": offset,
        "total": total,
        "next_offset": (
            next_offset
            if next_offset < total and next_offset <= MAX_PAGE_OFFSET
            else None
        ),
    }


class PlayersNotFoundError(LookupError):
    def __init__(self, missing_ids: tuple[str, ...]) -> None:
        self.missing_ids = missing_ids
        super().__init__("One or more requested player IDs were not found")


class PlayerQueryTimeoutError(TimeoutError):
    def __init__(self) -> None:
        super().__init__("Player database read deadline exceeded")


@dataclass
class _ReadConnection:
    connection: sqlite3.Connection
    busy_timeout_seconds: float
    deadline: Optional[float]
    interrupted: bool = False

    def check_deadline(self) -> None:
        if self.deadline is not None and monotonic() >= self.deadline:
            raise PlayerQueryTimeoutError()

    def interrupt_if_expired(self) -> int:
        if self.deadline is not None and monotonic() >= self.deadline:
            self.interrupted = True
            return 1
        return 0

    def execute(self, query: str, parameters: tuple = ()) -> sqlite3.Cursor:
        if self.deadline is not None:
            remaining = self.deadline - monotonic()
            if remaining <= 0:
                raise PlayerQueryTimeoutError()
            # SQLite restarts its busy wait per statement; share the read's budget.
            wait_ms = int(min(self.busy_timeout_seconds, remaining) * 1000)
            self.connection.execute(f"PRAGMA busy_timeout = {wait_ms}")
            self.check_deadline()
        return self.connection.execute(query, parameters)


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
                validate_integer(name, value, 1, 9999)
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
    def __init__(
        self,
        db_path=None,
        *,
        busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
        query_timeout_seconds: Optional[float] = None,
    ):
        validate_db_timeout("busy_timeout_seconds", busy_timeout_seconds, 0)
        if query_timeout_seconds is not None:
            validate_db_timeout(
                "query_timeout_seconds",
                query_timeout_seconds,
                MIN_QUERY_TIMEOUT_SECONDS,
            )
        self.busy_timeout_seconds = busy_timeout_seconds
        self.query_timeout_seconds = query_timeout_seconds
        self.db_path = Path(PLAYER_DB if db_path is None else db_path).resolve()
        self.columns = self.get_columns()

    @contextmanager
    def _read_connection(self, *, row_factory=None) -> Iterator[_ReadConnection]:
        deadline = (
            monotonic() + self.query_timeout_seconds
            if self.query_timeout_seconds is not None
            else None
        )
        connect_timeout = (
            min(self.busy_timeout_seconds, self.query_timeout_seconds)
            if self.query_timeout_seconds is not None
            else self.busy_timeout_seconds
        )
        with closing(
            sqlite3.connect(
                f"{self.db_path.as_uri()}?mode=ro", uri=True, timeout=connect_timeout
            )
        ) as conn:
            reader = _ReadConnection(conn, self.busy_timeout_seconds, deadline)
            conn.row_factory = row_factory
            try:
                if deadline is not None:
                    conn.set_progress_handler(
                        reader.interrupt_if_expired, _PROGRESS_HANDLER_STEPS
                    )
                reader.execute("PRAGMA query_only = ON")
                yield reader
                # Short statements may not reach a progress-handler checkpoint.
                reader.check_deadline()
            except sqlite3.OperationalError as exc:
                if reader.interrupted:
                    raise PlayerQueryTimeoutError() from exc
                raise
            finally:
                if deadline is not None:
                    conn.set_progress_handler(None, 0)

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

    def get_player(self, player_id: str) -> dict:
        validate_player_id(player_id)
        rows = self._fetchall(
            "SELECT * FROM players WHERE playerId COLLATE BINARY = ? LIMIT ?",
            (player_id, 2),
        )
        if len(rows) > 1:
            raise sqlite3.DataError("Player details require unique stored player IDs")
        if not rows:
            raise PlayersNotFoundError((player_id,))
        return self.convert_row_to_dict(rows[0])

    def search_players(
        self,
        filters: Optional[PlayerFilters] = None,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> dict:
        where_clause, parameters = self._filter_clause(filters)
        validate_integer("limit", limit, 1, MAX_PAGE_LIMIT)
        validate_integer("offset", offset, 0, MAX_PAGE_OFFSET)
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
        return {
            "data": [self.convert_row_to_dict(row) for row in rows],
            "pagination": _pagination(limit, offset, total),
        }

    def rank_players(
        self,
        metric: str,
        filters: Optional[PlayerFilters] = None,
        *,
        direction: str = "desc",
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> dict:
        columns = {"height": "height", "weight": "weight", "birthYear": "birthYear"}
        directions = {"asc": "ASC", "desc": "DESC"}
        if not isinstance(metric, str) or metric not in columns:
            raise ValueError("metric must be one of height, weight, birthYear")
        if not isinstance(direction, str) or direction not in directions:
            raise ValueError("direction must be one of asc, desc")
        where_clause, parameters = self._filter_clause(filters)
        validate_integer("limit", limit, 1, MAX_PAGE_LIMIT)
        validate_integer("offset", offset, 0, MAX_PAGE_OFFSET)
        column = columns[metric]
        order = directions[direction]
        ranked_where = (
            f"{where_clause} AND {column} IS NOT NULL"
            if where_clause
            else f" WHERE {column} IS NOT NULL"
        )
        with self._read_connection() as conn:
            # Validate and count the full population in the page's read snapshot.
            conn.execute("BEGIN")
            total, known_count, minimum, maximum = conn.execute(
                f"SELECT COUNT(*), COUNT({column}), MIN({column}), MAX({column}) "
                f"FROM players{where_clause}",
                parameters,
            ).fetchone()
            if known_count and any(
                not isinstance(value, (int, float)) or not isfinite(value)
                for value in (minimum, maximum)
            ):
                raise sqlite3.DataError(
                    f"{metric} rankings require finite numeric values"
                )
            rows = conn.execute(
                f"SELECT * FROM players{ranked_where} "
                f"ORDER BY {column} {order}, playerId COLLATE BINARY ASC LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
        return {
            "metric": metric,
            "direction": direction,
            "excluded_count": total - known_count,
            "data": [self.convert_row_to_dict(row) for row in rows],
            "pagination": _pagination(limit, offset, known_count),
        }

    def get_player_statistics(
        self,
        filters: Optional[PlayerFilters] = None,
        *,
        group_limit: int = DEFAULT_GROUP_LIMIT,
    ) -> dict:
        where_clause, parameters = self._filter_clause(filters)
        validate_integer("group_limit", group_limit, 1, MAX_GROUP_LIMIT)
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

        with self._read_connection(row_factory=sqlite3.Row) as conn:
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

    def compare_players(self, player_ids: Union[list[str], tuple[str, ...]]) -> dict:
        if not isinstance(player_ids, (list, tuple)):
            raise TypeError("player_ids must be a list or tuple of player IDs")
        if not 2 <= len(player_ids) <= MAX_COMPARISON_PLAYERS:
            raise ValueError(
                f"player_ids must contain between 2 and {MAX_COMPARISON_PLAYERS} IDs"
            )
        player_ids = tuple(player_ids)
        for player_id in player_ids:
            validate_player_id(player_id)
        if len(set(player_ids)) != len(player_ids):
            raise ValueError("player_ids must contain distinct IDs")

        placeholders = ", ".join("?" for _ in player_ids)
        # One extra row detects duplicate stored IDs without an unbounded fetch.
        rows = self._fetchall(
            f"SELECT * FROM players WHERE playerId COLLATE BINARY IN ({placeholders}) "
            "LIMIT ?",
            (*player_ids, len(player_ids) + 1),
        )
        records = {}
        for row in rows:
            record = self.convert_row_to_dict(row)
            if record["playerId"] in records:
                raise sqlite3.DataError("Comparisons require unique stored player IDs")
            records[record["playerId"]] = record
        missing_ids = tuple(
            player_id for player_id in player_ids if player_id not in records
        )
        if missing_ids:
            raise PlayersNotFoundError(missing_ids)
        players = [records[player_id] for player_id in player_ids]

        numeric_fields = ("birthYear", "height", "weight")
        for player in players:
            for field in numeric_fields:
                value = player[field]
                if value is not None and (
                    not isinstance(value, (int, float)) or not isfinite(value)
                ):
                    raise sqlite3.DataError(
                        f"{field} comparisons require finite numeric values"
                    )

        differences = []
        for left, right in combinations(players, 2):
            numeric = {}
            for field in numeric_fields:
                left_value, right_value = left[field], right[field]
                difference = (
                    left_value - right_value
                    if left_value is not None and right_value is not None
                    else None
                )
                if difference is not None and not isfinite(difference):
                    raise sqlite3.DataError(
                        f"{field} comparisons require finite numeric differences"
                    )
                numeric[field] = difference
            differences.append(
                {
                    "left_player_id": left["playerId"],
                    "right_player_id": right["playerId"],
                    "numeric": numeric,
                }
            )
        return {"data": players, "differences": differences}

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
