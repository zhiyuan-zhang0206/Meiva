# meiva:note
# One recorded debt and the two spellings a manifest writes it in: a bare count, or a table naming the
# owner paying it down and the condition it waits for. Both read back as one `DebtEntry`, so a caller
# never has to ask which one it got. The same module answers how many occurrences a value carries, for
# the readers that see a manifest as text, and how much of a table names no owner.

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeGuard

__all__ = [
    "SHAPES",
    "UNREVIEWED",
    "DebtEntry",
    "count_of",
    "entry_value",
    "has_shape",
    "is_table",
    "read_entry",
    "waits_for_nothing",
]

# meiva:note
# The prefix an `until` carries while nobody has read it: what `debt record` writes when its caller
# stated no condition, so an entry can land without claiming a reason it does not have. It is one of
# the three states an entry can be in -- no `until`, an unreviewed one, no `owner` -- and the summary
# counts them apart, so a mechanically recorded entry stays visible until a person replaces the
# sentence.
UNREVIEWED = "unreviewed:"

# meiva:note
# The two shapes a stated `until` takes. They are different words on purpose: one says something will
# arrive, the other that nothing will, and two answers to different questions must not read alike --
# a reader who cannot tell which one a sentence is stops checking it.
SHAPES = ("waits for ", "permanently:")

_FIELDS = ("count", "owner", "until")


# meiva:note
# Whether an `until` is a sentence this tool can read: one of the two stated shapes, or the marker that
# says nobody has read it yet. The marker passes so `record` can write an entry that is visibly
# unreviewed rather than inventing a reason; the summary counts those apart.
def has_shape(until: str) -> bool:
    return until.startswith((*SHAPES, UNREVIEWED))


# meiva:note
# Whether a sentence picks the waiting shape and then says nothing will arrive. `waits for nothing
# ...` names no condition that could arrive, so it is the other shape's answer written in this one's
# words, and a reader cannot tell which of the two answers the entry gives. Only the head before the
# colon is read, because the head is where a wait names what it waits for; the explanation after it
# may argue with anything, "nothing" included.
def waits_for_nothing(until: str) -> bool:
    head = until.split(":", 1)[0]
    return head.startswith(SHAPES[0]) and "nothing" in head.lower()


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
