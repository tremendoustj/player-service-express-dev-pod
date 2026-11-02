import logging

from sqlalchemy.exc import SQLAlchemyError


def _load_application():
    try:
        from app import app
    except (ImportError, OSError, ValueError, SQLAlchemyError):
        logging.getLogger("app").exception("application_startup_failed")
        # Gunicorn also prints boot exception messages directly to stderr.
        raise RuntimeError(
            "Player Service startup failed; inspect structured logs"
        ) from None
    return app


app = _load_application()
