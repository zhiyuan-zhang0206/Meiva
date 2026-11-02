# meiva:note
# The three states a recorded entry can be in, counted from the `[debt]` tables and nothing else: no
# `owner`, no `until`, and an `until` a machine wrote that no person has read. They sit in one module
# because they are read together and none may mask another -- an entry with a placeholder is not
# missing an `until`, and an entry with neither is counted in both.

from __future__ import annotations

from collections.abc import Iterable, Mapping

from meizel.contracts.debt.entry import UNREVIEWED, DebtEntry
from meizel.graph.model import Code

__all__ = ["unowned", "unreviewed", "without_until"]


# meiva:note
# The entries that name no owner — the ones a backfill has to reach — and the occurrences they carry.
def unowned(table: Mapping[Code, Mapping[str, DebtEntry]]) -> tuple[int, int]:
    return _count(entry for rows in table.values() for entry in rows.values() if not entry.owner)


# meiva:note
# The entries that state no exit condition at all: what `check` reports as `bare-debt` beside an
# entry naming no owner, and the number `debt` prints beside it.
def without_until(table: Mapping[Code, Mapping[str, DebtEntry]]) -> tuple[int, int]:
    return _count(entry for rows in table.values() for entry in rows.values() if not entry.until)


# meiva:note
# The entries carrying the placeholder `record` writes: recorded, but with an exit condition nobody
# has read. Counted apart from a missing `until` so the placeholder cannot pass for a reason.
def unreviewed(table: Mapping[Code, Mapping[str, DebtEntry]]) -> tuple[int, int]:
    return _count(
        entry
        for rows in table.values()
        for entry in rows.values()
        if entry.until.startswith(UNREVIEWED)
    )


def _count(entries: Iterable[DebtEntry]) -> tuple[int, int]:
    found = list(entries)
    return len(found), sum(entry.count for entry in found)
