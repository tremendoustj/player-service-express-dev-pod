import hmac
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from typing import Mapping, Optional
from urllib.parse import parse_qsl
from uuid import uuid4

from flask import Blueprint, Flask, Response, g, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST
from werkzeug.exceptions import HTTPException

from express_metrics import ExpressMetrics
from player_service import (
    DEFAULT_GROUP_LIMIT,
    DEFAULT_PAGE_LIMIT,
    MAX_GROUP_LIMIT,
    MAX_PAGE_LIMIT,
    MAX_PAGE_OFFSET,
    MIN_QUERY_TIMEOUT_SECONDS,
    PlayerFilters,
    PlayerQueryTimeoutError,
    PlayersNotFoundError,
    PlayerService,
    validate_comparison_ids,
    validate_db_timeout,
    validate_integer,
    validate_player_id,
    validate_ranking,
)


API_PREFIX = "/integrations/express/v1"
MAX_QUERY_STRING_BYTES = 4096
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43,256}")
_AUTH_PATTERN = re.compile(r"(?i:Bearer) ([A-Za-z0-9_-]{43,256})")
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_INTEGER_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,5})")
_INVALID_PERCENT_ESCAPE = re.compile(rb"%(?![0-9A-Fa-f]{2})")
_FILTER_PARAMETERS = frozenset(
    (
        "q",
        "birth_country",
        "birth_year_min",
        "birth_year_max",
        "bats",
        "throws",
    )
)
_SEARCH_PARAMETERS = _FILTER_PARAMETERS | {"limit", "offset"}
_STATISTICS_PARAMETERS = _FILTER_PARAMETERS | {"group_limit"}
_RANKING_PARAMETERS = _SEARCH_PARAMETERS | {"metric", "direction"}
_COMPARISON_PARAMETERS = frozenset(("player_ids",))


@dataclass(frozen=True)
class IntegrationSettings:
    token: str = field(repr=False)
    busy_timeout_seconds: float = 1.0
    query_timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not _TOKEN_PATTERN.fullmatch(self.token):
            raise ValueError(
                "PLAYER_INTEGRATION_TOKEN must contain 43-256 URL-safe ASCII "
                "letters, digits, hyphens, or underscores"
            )
        validate_db_timeout(
            "PLAYER_DB_BUSY_TIMEOUT_SECONDS", self.busy_timeout_seconds, 0
        )
        validate_db_timeout(
            "PLAYER_QUERY_TIMEOUT_SECONDS",
            self.query_timeout_seconds,
            MIN_QUERY_TIMEOUT_SECONDS,
        )

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str]
    ) -> Optional["IntegrationSettings"]:
        enabled = environ.get("PLAYER_INTEGRATION_ENABLED", "false")
        if enabled not in ("true", "false"):
            raise ValueError("PLAYER_INTEGRATION_ENABLED must be true or false")
        if enabled == "false":
            return None

        timeouts = {}
        for name, default in (
            ("PLAYER_DB_BUSY_TIMEOUT_SECONDS", "1"),
            ("PLAYER_QUERY_TIMEOUT_SECONDS", "2"),
        ):
            try:
                timeouts[name] = float(environ.get(name, default))
            except ValueError:
                raise ValueError(f"{name} must be a finite number of seconds") from None
        return cls(
            token=environ.get("PLAYER_INTEGRATION_TOKEN", ""),
            busy_timeout_seconds=timeouts["PLAYER_DB_BUSY_TIMEOUT_SECONDS"],
            query_timeout_seconds=timeouts["PLAYER_QUERY_TIMEOUT_SECONDS"],
        )


def _in_integration_scope() -> bool:
    return request.path == API_PREFIX or request.path.startswith(API_PREFIX + "/")


def _error(status: int, code: str, message: str) -> Response:
    g.express_error_code = code
    response = jsonify(
        error={"code": code, "message": message},
        request_id=g.express_request_id,
    )
    response.status_code = status
    if status == 401:
        response.headers["WWW-Authenticate"] = 'Bearer realm="player-service-express"'
    return response


def _integer(name: str, value: str, minimum: int, maximum: int) -> int:
    if not _INTEGER_PATTERN.fullmatch(value):
        raise ValueError(
            f"{name} must use unsigned decimal notation without leading zeros"
        )
    number = int(value)
    validate_integer(name, number, minimum, maximum)
    return number


def _query_arguments(allowed_parameters: frozenset[str]) -> dict[str, str]:
    raw_query = request.query_string
    if _INVALID_PERCENT_ESCAPE.search(raw_query):
        raise ValueError("Query parameters must use valid UTF-8 percent encoding")
    try:
        pairs = parse_qsl(
            raw_query.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=len(allowed_parameters),
        )
    except ValueError:
        raise ValueError("Query parameters are malformed or too numerous") from None
    arguments = {}
    for name, value in pairs:
        if name not in allowed_parameters:
            raise ValueError("Unknown query parameter")
        if name in arguments:
            raise ValueError("Query parameters must not be repeated")
        arguments[name] = value
    return arguments


def _player_filters(arguments: Mapping[str, str]) -> PlayerFilters:
    minimum = arguments.get("birth_year_min")
    maximum = arguments.get("birth_year_max")
    return PlayerFilters(
        q=arguments.get("q"),
        birth_country=arguments.get("birth_country"),
        birth_year_min=(
            _integer("birth_year_min", minimum, 1, 9999) if minimum is not None else None
        ),
        birth_year_max=(
            _integer("birth_year_max", maximum, 1, 9999) if maximum is not None else None
        ),
        bats=arguments.get("bats"),
        throws=arguments.get("throws"),
    )


def _pagination_arguments(arguments: Mapping[str, str]) -> tuple[int, int]:
    limit = _integer(
        "limit", arguments.get("limit", str(DEFAULT_PAGE_LIMIT)), 1, MAX_PAGE_LIMIT
    )
    offset = _integer("offset", arguments.get("offset", "0"), 0, MAX_PAGE_OFFSET)
    return limit, offset


def _search_arguments() -> tuple[PlayerFilters, int, int]:
    arguments = _query_arguments(_SEARCH_PARAMETERS)
    filters = _player_filters(arguments)
    limit, offset = _pagination_arguments(arguments)
    return filters, limit, offset


def configure_express_api(app: Flask) -> None:
    settings = IntegrationSettings.from_environment(os.environ)
    if settings is None:
        return
    metrics = ExpressMetrics()
    app.extensions["express_metrics"] = metrics
    api = Blueprint("express_integration", __name__, url_prefix=API_PREFIX)

    def player_service() -> PlayerService:
        return PlayerService(
            busy_timeout_seconds=settings.busy_timeout_seconds,
            query_timeout_seconds=settings.query_timeout_seconds,
        )

    def success_response(result: dict, *, result_count: int) -> Response:
        try:
            body = app.json.dumps(
                {**result, "request_id": g.express_request_id}, allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise sqlite3.DataError("Player data must be valid JSON") from exc
        g.express_result_count = result_count
        return app.response_class(body, mimetype="application/json")

    @app.before_request
    def prepare_express_request():
        if not _in_integration_scope():
            return None
        supplied_id = request.headers.get("X-Request-ID", "")
        g.express_request_id = (
            supplied_id if _REQUEST_ID_PATTERN.fullmatch(supplied_id) else str(uuid4())
        )
        g.express_started_at = monotonic()
        authorization = _AUTH_PATTERN.fullmatch(request.headers.get("Authorization", ""))
        if authorization is None or not hmac.compare_digest(
            authorization.group(1), settings.token
        ):
            return _error(401, "unauthorized", "A valid bearer token is required")
        if len(request.query_string) > MAX_QUERY_STRING_BYTES:
            return _error(414, "request_too_large", "Query string exceeds 4096 bytes")
        # WSGI's input_terminated flag advertises safe EOF, not a body's presence.
        if request.method in ("GET", "HEAD") and (
            request.content_length not in (None, 0)
            or "Transfer-Encoding" in request.headers
        ):
            return _error(400, "invalid_request", "Request bodies are not supported")
        return None

    @api.get("/players")
    def search_players():
        try:
            filters, limit, offset = _search_arguments()
        except ValueError as exc:
            return _error(400, "invalid_request", str(exc))
        result = player_service().search_players(filters, limit=limit, offset=offset)
        return success_response(result, result_count=len(result["data"]))

    @api.get("/players/<string:player_id>")
    def get_player(player_id: str):
        try:
            validate_player_id(player_id)
            if request.query_string:
                raise ValueError("Query parameters are not supported")
        except ValueError as exc:
            return _error(400, "invalid_request", str(exc))
        result = player_service().get_player(player_id)
        return success_response({"data": result}, result_count=1)

    @api.get("/statistics")
    def get_player_statistics():
        try:
            arguments = _query_arguments(_STATISTICS_PARAMETERS)
            filters = _player_filters(arguments)
            group_limit = _integer(
                "group_limit",
                arguments.get("group_limit", str(DEFAULT_GROUP_LIMIT)),
                1,
                MAX_GROUP_LIMIT,
            )
        except ValueError as exc:
            return _error(400, "invalid_request", str(exc))
        result = player_service().get_player_statistics(filters, group_limit=group_limit)
        return success_response(result, result_count=result["total"])

    @api.get("/rankings")
    def rank_players():
        try:
            arguments = _query_arguments(_RANKING_PARAMETERS)
            metric = arguments.get("metric", "")
            direction = arguments.get("direction", "desc")
            validate_ranking(metric, direction)
            filters = _player_filters(arguments)
            limit, offset = _pagination_arguments(arguments)
        except ValueError as exc:
            return _error(400, "invalid_request", str(exc))
        result = player_service().rank_players(
            metric, filters, direction=direction, limit=limit, offset=offset
        )
        return success_response(result, result_count=len(result["data"]))

    @api.get("/comparisons")
    def compare_players():
        try:
            arguments = _query_arguments(_COMPARISON_PARAMETERS)
            player_ids = validate_comparison_ids(
                arguments.get("player_ids", "").split(",")
            )
        except ValueError as exc:
            return _error(400, "invalid_request", str(exc))
        result = player_service().compare_players(player_ids)
        return success_response(result, result_count=len(result["data"]))

    @api.get("/metrics")
    def get_metrics():
        if request.query_string:
            return _error(400, "invalid_request", "Query parameters are not supported")
        return app.response_class(metrics.render(), content_type=CONTENT_TYPE_LATEST)

    @api.errorhandler(PlayersNotFoundError)
    def players_not_found(exc):
        g.express_exception_type = type(exc).__name__
        return _error(
            404, "player_not_found", "One or more requested players were not found"
        )

    @api.errorhandler(PlayerQueryTimeoutError)
    def query_timeout(exc):
        g.express_exception_type = type(exc).__name__
        return _error(504, "query_timeout", "Player database read deadline exceeded")

    @api.errorhandler(sqlite3.Error)
    def storage_error(exc):
        g.express_exception_type = type(exc).__name__
        return _error(503, "storage_unavailable", "Player data is temporarily unavailable")

    @api.errorhandler(Exception)
    def unexpected_error(exc):
        g.express_exception_type = type(exc).__name__
        return _error(500, "internal_error", "An unexpected error occurred")

    @app.errorhandler(HTTPException)
    def http_error(exc):
        if not _in_integration_scope():
            return exc
        code = {400: "invalid_request", 404: "not_found", 405: "method_not_allowed"}.get(
            exc.code, "http_error"
        )
        error_response = _error(exc.code or 500, code, exc.name)
        # Preserve protocol headers, including Allow and redirect locations.
        response = exc.get_response()
        response.set_data(error_response.get_data())
        response.content_type = error_response.content_type
        return response

    @app.after_request
    def finish_express_request(response):
        if not _in_integration_scope():
            return response
        response.headers["X-Request-ID"] = g.express_request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        matched = request.blueprint == api.name and request.url_rule is not None
        duration_seconds = monotonic() - g.express_started_at
        event = {
            "event": "express_request",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "operation": request.endpoint if matched else "unmatched",
            "route": request.url_rule.rule if matched else "unmatched",
            "status": response.status_code,
            "duration_ms": round(duration_seconds * 1000, 3),
            "request_id": g.express_request_id,
            "result_count": g.get("express_result_count", 0),
            "error_code": g.get("express_error_code"),
            "exception_type": g.get("express_exception_type"),
        }
        metrics.observe(
            operation=event["operation"],
            status=response.status_code,
            duration_seconds=duration_seconds,
            error_code=event["error_code"],
        )
        level = logging.WARNING if response.status_code >= 400 else logging.INFO
        app.logger.log(level, json.dumps(event, separators=(",", ":")))
        return response

    app.register_blueprint(api)
