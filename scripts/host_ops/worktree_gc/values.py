# meiva:note
# Boundary values: JSON written by another program, a line of `git` output, a CSV-ish ledger row.
# Every reader here answers one field as `object` and narrows it once, so the rest of the package
# holds real types instead of `Any`. `scripts/host_ops/docs/worktree-gc.meiva.okf.md` owns why the
# ledger and the probe are read rather than trusted.

from __future__ import annotations

from contextlib import suppress

from base.host.boundary.json_values import is_object_dict, is_object_list

__all__ = ["float_of", "int_of", "object_dict", "object_rows", "text_of"]


def text_of(value: object) -> str:
    # meiva:note
    # A string field, or the empty string when it is absent or not a string.
    return value if isinstance(value, str) else ""


def float_of(value: object) -> float:
    # meiva:note
    # A number field as a float; anything else reads as zero, which is the "never happened"
    # sentinel the cache files start from.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        with suppress(ValueError):
            return float(value)
    return 0.0


def int_of(value: object) -> int:
    # meiva:note
    # A count field as an int; a bool is a flag, not a count, so it reads as zero.
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        with suppress(ValueError):
            return int(value)
    return 0


def object_dict(value: object) -> dict[str, object]:
    # meiva:note
    # A JSON object as a mutable `dict[str, object]`; an array or a scalar reads as empty.
    return dict(value) if is_object_dict(value) else {}


def object_rows(value: object) -> list[dict[str, object]]:
    # meiva:note
    # A JSON array of objects, dropping the entries that are not objects: one malformed row must
    # not cost the round the rows that are readable.
    return [dict(row) for row in value if is_object_dict(row)] if is_object_list(value) else []
