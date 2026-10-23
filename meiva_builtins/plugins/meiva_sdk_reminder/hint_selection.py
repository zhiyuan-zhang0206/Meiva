# meiva:note
# The after_exec reminder decisions as values: which categories hint for one cell, the compaction
# re-arm of the reminded set, and the NameError that names a variable an earlier cell defined.
from __future__ import annotations

import builtins
import keyword
import re
from collections.abc import Collection
from dataclasses import dataclass

from ._state import mentions_watcher

_NAMEERROR_CATEGORY_PREFIX = "nameerror:"

_NAMEERROR_RE = re.compile(r"(?:^|\n)NameError: name '([^'\n]+)' is not defined\b")


@dataclass(frozen=True)
class HintSelection:
    hinted: tuple[str, ...]
    newly_seen: frozenset[str]
    nameerror_hint: str | None


# meiva:note
# The undefined name of a NameError output, only when an earlier code cell mentions that name:
# the agent assumed a variable persisted between cells.
def assumed_persistence_name(output: str, earlier_codes: Collection[str]) -> str | None:
    match = _NAMEERROR_RE.search(output)
    if match is None:
        return None
    name = match.group(1)
    if not name.isidentifier() or keyword.iskeyword(name) or name in vars(builtins):
        return None
    whole_name = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
    return name if any(whole_name.search(code) for code in earlier_codes) else None


def _nameerror_persistence_hint(name: str) -> str:
    return (
        f"NameError: '{name}' appeared in an earlier execute_code call, "
        "but each call runs in a fresh interpreter — variables do not persist "
        "between calls. Re-define it here, or carry state via files or shell sessions."
    )


# meiva:note
# The reminded set and bookmark after a compaction re-arm: once the compact version has moved past
# the bookmark, the messages that carried the earlier hints are summarized away, so the set is empty
# and the bookmark catches up.
def rearmed_reminded(
    reminded: Collection[str], bookmark: int, compact_version: int
) -> tuple[frozenset[str], int]:
    if compact_version > bookmark:
        return frozenset(), compact_version
    return frozenset(reminded), bookmark


# meiva:note
# Which categories hint for one cell. A cell that sleeps while already naming `watcher` is working
# with the watcher primitive itself, so its wait category is marked seen without a hint. Under
# `every_time` the reminded set does not gate the code categories; a NameError name still hints
# once per window.
def select_hints(
    code: str,
    matched: Collection[str],
    reminded: Collection[str],
    nameerror_name: str | None,
    *,
    every_time: bool,
) -> HintSelection:
    silent = {"wait"} if "wait" in matched and mentions_watcher(code) else set[str]()
    if every_time:
        hinted = tuple(cat for cat in matched if cat not in silent)
    else:
        hinted = tuple(cat for cat in matched if cat not in reminded and cat not in silent)
    newly_seen = set(hinted) | (silent - set(reminded))
    nameerror_hint = None
    if nameerror_name is not None:
        nameerror_category = f"{_NAMEERROR_CATEGORY_PREFIX}{nameerror_name}"
        if nameerror_category not in reminded:
            newly_seen.add(nameerror_category)
            nameerror_hint = _nameerror_persistence_hint(nameerror_name)
    return HintSelection(hinted, frozenset(newly_seen), nameerror_hint)
