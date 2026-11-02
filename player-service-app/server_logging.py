import json
import logging
from datetime import datetime, timezone


class ProductionJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "process_id": record.process,
        }
        request_event = getattr(record, "express_event", None)
        if request_event is not None:
            event.update(request_event)
        else:
            # Server parser errors can embed raw URIs, headers, and credentials.
            event.update(
                event="server_log",
                source=f"{record.module}.{record.funcName}:{record.lineno}",
                exception_type=(
                    record.exc_info[0].__name__
                    if record.exc_info and record.exc_info[0] is not None
                    else None
                ),
            )
        return json.dumps(event, allow_nan=False, separators=(",", ":"))


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": ProductionJsonFormatter}},
    "handlers": {
        "json_stdout": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "stream": "ext://sys.stdout",
        },
        "discard_access": {"class": "logging.NullHandler"},
    },
    "root": {"level": "WARNING", "handlers": ["json_stdout"]},
    "loggers": {
        "app": {
            "level": "INFO",
            "handlers": ["json_stdout"],
            "propagate": False,
        },
        "gunicorn.error": {
            "level": "INFO",
            "handlers": ["json_stdout"],
            "propagate": False,
        },
        "gunicorn.access": {
            "level": "INFO",
            "handlers": ["discard_access"],
            "propagate": False,
        },
    },
}
