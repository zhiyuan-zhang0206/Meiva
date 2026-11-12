from __future__ import annotations

from datetime import UTC, datetime

from base.lm.harness.codex.transcript.errors import MissingFieldError


def iso_at(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="milliseconds")


def parse_iso_ms(stamp: str, field: str) -> int:
    try:
        return round(datetime.fromisoformat(stamp).timestamp() * 1000)
    except ValueError:
        raise MissingFieldError(field) from None
