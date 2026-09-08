"""Tests for structured logging configuration."""

import json

import pytest
import structlog

from vortex_ai_gateway.config import Settings
from vortex_ai_gateway.logging_config import configure_logging


def _lines(captured: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in captured.splitlines() if line.strip()]


def test_emits_one_json_object_per_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(_env_file=None, log_level="INFO"))

    structlog.get_logger().info("hello", key="value")

    (record,) = _lines(capsys.readouterr().out)
    assert record["event"] == "hello"
    assert record["key"] == "value"
    assert record["level"] == "info"
    assert "timestamp" in record


def test_context_vars_are_merged_into_every_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(Settings(_env_file=None))

    structlog.contextvars.bind_contextvars(request_id="abc123")
    structlog.get_logger().info("with context")

    (record,) = _lines(capsys.readouterr().out)
    assert record["request_id"] == "abc123"


def test_log_level_filters_below_threshold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(Settings(_env_file=None, log_level="WARNING"))

    log = structlog.get_logger()
    log.info("suppressed")
    log.warning("kept")

    records = _lines(capsys.readouterr().out)
    assert [r["event"] for r in records] == ["kept"]
