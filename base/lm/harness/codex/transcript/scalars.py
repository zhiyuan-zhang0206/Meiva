from __future__ import annotations

from base.lm.harness.codex.transcript.errors import MissingFieldError
from base.lm.harness.codex.transcript.shapes import Obj


def text(row: Obj, field: str) -> str:
    value = row.get(field)
    if isinstance(value, str):
        return value
    raise MissingFieldError(field)


def opt_text(row: Obj, field: str) -> str | None:
    value = row.get(field)
    return value if isinstance(value, str) else None


def opt_int(row: Obj, field: str) -> int | None:
    value = row.get(field)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def req_int(row: Obj, field: str) -> int:
    value = opt_int(row, field)
    if value is None:
        raise MissingFieldError(field)
    return value
