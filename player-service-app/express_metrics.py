import os
from typing import Optional

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest


_OPERATIONS = frozenset(
    "express_integration." + name
    for name in (
        "search_players",
        "get_player",
        "get_player_statistics",
        "rank_players",
        "compare_players",
        "get_metrics",
    )
)
_ERROR_CODES = frozenset(
    (
        "invalid_request",
        "unauthorized",
        "not_found",
        "player_not_found",
        "method_not_allowed",
        "request_too_large",
        "storage_unavailable",
        "query_timeout",
        "internal_error",
        "http_error",
    )
)
_LATENCY_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120,
)


class ExpressMetrics:
    def __init__(self) -> None:
        if any(
            name in os.environ
            for name in ("PROMETHEUS_MULTIPROC_DIR", "prometheus_multiproc_dir")
        ):
            raise ValueError(
                "Express metrics require single-process collection; "
                "PROMETHEUS_MULTIPROC_DIR and prometheus_multiproc_dir are not supported"
            )
        self.registry = CollectorRegistry()
        self._requests = Counter(
            "player_service_express_requests_total",
            "Completed integration HTTP requests, including metrics scrapes.",
            ("operation", "status"),
            registry=self.registry,
        )
        self._errors = Counter(
            "player_service_express_errors_total",
            "Integration HTTP errors, including authentication failures and timeouts.",
            ("operation", "code"),
            registry=self.registry,
        )
        self._duration = Histogram(
            "player_service_express_request_duration_seconds",
            "Integration request time through response preparation, excluding transport.",
            ("operation",),
            buckets=_LATENCY_BUCKETS,
            registry=self.registry,
        )

    def observe(
        self,
        *,
        operation: Optional[str],
        status: int,
        duration_seconds: float,
        error_code: Optional[str],
    ) -> None:
        operation = operation if operation in _OPERATIONS else "unmatched"
        status_label = str(status) if 100 <= status <= 599 else "other"
        self._requests.labels(operation=operation, status=status_label).inc()
        self._duration.labels(operation=operation).observe(duration_seconds)
        if status >= 400:
            code = error_code if error_code in _ERROR_CODES else "http_error"
            self._errors.labels(operation=operation, code=code).inc()

    def render(self) -> bytes:
        return generate_latest(self.registry)
