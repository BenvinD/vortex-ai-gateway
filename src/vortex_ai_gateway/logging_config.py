"""Structured logging configuration.

Every log line is one JSON object written to stdout, so the platform's log
collector can ship it without a parsing rule. Request-scoped fields — notably
``request_id``, bound by
:class:`~vortex_ai_gateway.middleware.RequestIDMiddleware` — are merged in from
context variables, so a call site never has to thread them through by hand.
"""

import logging

import structlog

from vortex_ai_gateway.config import Settings

_DEFAULT_LEVEL = logging.INFO


def _resolve_level(name: str) -> int:
    """Map a level name (``"INFO"``, ``"debug"``, ...) to its numeric value."""
    return logging.getLevelNamesMapping().get(name.strip().upper(), _DEFAULT_LEVEL)


def configure_logging(settings: Settings) -> None:
    """Point structlog at stdout as JSON, filtered to ``settings.log_level``.

    Idempotent: the last call wins. The app factory calls it at startup; tests
    call it once per app instance.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_resolve_level(settings.log_level)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
