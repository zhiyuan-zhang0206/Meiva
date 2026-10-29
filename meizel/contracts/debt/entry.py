# meiva:note
# One recorded debt and the two spellings a manifest writes it in: a bare count, or a table naming the
# owner paying it down and the condition it waits for. Both read back as one `DebtEntry`, so a caller
# never has to ask which one it got. The same module answers how many occurrences a value carries, for
# the readers that see a manifest as text, and how much of a table names no owner.

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeGuard

from meizel.graph.model import Code

__all__ = ["DebtEntry", "count_of", "entry_value", "is_table", "read_entry", "unowned"]

_FIELDS = ("count", "owner", "until")


# meiva:note
# One recorded debt: how many occurrences, who is paying it down, and what it waits for.
@dataclass(frozen=True, slots=True)
class DebtEntry:
    count: int
    owner: str = ""
    until: str = ""


# meiva:note
# A parsed TOML table whose keys are all strings, in the two steps the checker's own predicate takes:
# one `isinstance` to a table of objects, then the keys — a single step leaves the key type unknown,
# and a `cast` would be recorded as debt in a strict module.
def _is_object_table(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def is_table(value: object) -> TypeGuard[dict[str, object]]:
    return _is_object_table(value) and all(isinstance(key, str) for key in value)


# meiva:note
# One entry as the manifest writes it: a bare count, or a table naming its owner and what it waits
# for. An unknown field is reported rather than dropped, so a misspelt `owner` cannot read as "no
# owner" — the one thing this record exists to make visible.
def read_entry(value: object, where: str, report: Callable[[str], None]) -> DebtEntry | None:
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 1:
            report(f"{where}: the count must be a positive integer")
            return None
        return DebtEntry(value)
    if not is_table(value):
        report(f"{where}: the count must be a positive integer, or a table with `count` in it")
        return None
    unknown = sorted(set(value) - set(_FIELDS))
    if unknown:
        fields = ", ".join(_FIELDS)
        report(f"{where}: {', '.join(unknown)} is not an entry field; the fields are {fields}")
        return None
    count = value.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        report(f"{where}: the count must be a positive integer")
        return None
    owner = "" if value.get("owner") is None else _text(value["owner"], "owner", where, report)
    until = "" if value.get("until") is None else _text(value["until"], "until", where, report)
    if owner is None or until is None:
        return None
    return DebtEntry(count, owner, until)


def _text(value: object, field: str, where: str, report: Callable[[str], None]) -> str | None:
    if not isinstance(value, str):
        report(f"{where}: {field} must be a string")
        return None
    if not value.strip():
        report(f"{where}: {field} must not be empty; leave it out instead")
        return None
    return value.strip()


# meiva:note
# The count a `[debt]` value carries: a parsed entry, a bare integer, or an entry table read from a
# manifest's text. Anything else counts as nothing, so a malformed entry cannot inflate a total.
def count_of(value: object) -> int:
    if isinstance(value, DebtEntry):
        return value.count
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if is_table(value):
        count = value.get("count")
        if isinstance(count, int) and not isinstance(count, bool):
            return count
    return 0


# meiva:note
# The TOML value an entry is written as: a bare count when it has no metadata, so a baseline that
# never names an owner stays byte-identical. Both the writer and the round-trip check read this.
def entry_value(entry: DebtEntry) -> int | dict[str, object]:
    if not entry.owner and not entry.until:
        return entry.count
    value: dict[str, object] = {"count": entry.count}
    if entry.owner:
        value["owner"] = entry.owner
    if entry.until:
        value["until"] = entry.until
    return value


# meiva:note
# How much recorded debt names no owner: the entries to backfill, and the occurrences they carry.
def unowned(table: Mapping[Code, Mapping[str, DebtEntry]]) -> tuple[int, int]:
    missing = [entry for rows in table.values() for entry in rows.values() if not entry.owner]
    return len(missing), sum(entry.count for entry in missing)
