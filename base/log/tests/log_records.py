"""Capture loguru output in tests (pytest's `caplog` only sees stdlib logging)."""

from collections.abc import Iterator
from typing import Any, TypeGuard

import pytest

__all__ = ["LogRecords", "loguru_records"]


# meiva:note
# A record's `extra`: loguru keys it by the keyword names of the log call.
def _is_extra(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


class LogRecords(list[dict[str, Any]]):
    """The loguru records emitted since the fixture was requested, in order.

    A plain list of loguru record dicts (`message`, `level`, `extra`, ...), so existing
    `any("x" in r["message"] for r in loguru_records)` assertions keep working; the two readers
    cover the common assertions. Keyword arguments of the log call (`logger.warning("m", event="x")`)
    arrive in `record["extra"]`, so a structured field is asserted directly."""

    def messages(self, level: str | None = None) -> list[str]:
        """The formatted message of every record, optionally only those of one level name."""
        return [r["message"] for r in self if level is None or r["level"].name == level]

    # meiva:note
    # The `extra` mapping of every record, optionally only those logged with `event=<event>`
    # and/or at the level named `level` (as `messages` selects).
    def extras(
        self, *, event: str | None = None, level: str | None = None
    ) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        for record in self:
            extra = record.get("extra")
            if not _is_extra(extra):
                continue
            level_name = getattr(record.get("level"), "name", None)
            if (level is None or level_name == level) and (
                event is None or extra.get("event") == event
            ):
                found.append(extra)
        return found

    def events(self, name: str) -> list[dict[str, Any]]:
        """The records logged with `event=<name>` (the structured-event convention)."""
        return [r for r in self if r["extra"].get("event") == name]


@pytest.fixture
def loguru_records() -> Iterator[LogRecords]:
    """Capture loguru output for tests. pytest's built-in caplog captures stdlib logging,
    loguru doesn't go through stdlib, requires separate bridging.

    Usage:
        def test_foo(loguru_records):
            do_something_that_logs()
            assert loguru_records.messages("WARNING") == ["expected"]
    """
    from loguru import logger as _logger

    captured = LogRecords()
    handler_id = _logger.add(lambda msg: captured.append(dict(msg.record)), level="DEBUG")
    try:
        yield captured
    finally:
        _logger.remove(handler_id)
