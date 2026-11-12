# meiva:note
# The page ledger's pure core: records, reconciliation and the close decisions.
#
# A page this stack created has a hard deadline and an owner. Both are kept as one
# record per Chrome page target (``PageRecord``, keyed by the CDP target id, which
# is stable for the life of the Chrome process) so they survive a browser-mcp
# daemon restart; chrome-devtools-mcp's own page numbers do not (they restart with
# the upstream process). The effects -- reading Chrome, closing a target, the file
# -- live in ``page_lifecycle``; this module only decides.
#
# The record is the one basis for closing a page, and it is written when the stack
# CREATES the page (an isolated-context page too). The ``Meiva #<id>`` tab group is
# only the check that the user has not taken the page: a record is ``grouped`` once
# Chrome has shown the page in its owner's group, and from then on a page that is
# no longer in that group was taken by the user -- its record is dropped and the
# page is never closed. A record that never became grouped (an isolated context, a
# grouping that failed) is closed at its deadline.
#
# A page in an agent group with no record is NOT adopted as a rule: the tab-group
# worker files tabs by URL when it restores a layout, so a user's tab can sit in an
# agent group, and closing it on that evidence would close the user's tab. Two
# situations with evidence are handled: (a) after a Chrome restart a record whose
# target is gone passes to the one page in the same owner's group with the same URL
# (the restored tab), when both are unique and no other page of Chrome has that
# URL; (b) the first reconciliation of a
# ledger that does not exist yet (the first run of this mechanism) adopts the pages
# that are in agent groups at that moment, once, with a fresh default TTL.
#
# Deadlines are wall-clock epoch seconds. A monotonic clock does not survive the
# process, and this ledger exists to outlive it. The price is that a wall-clock
# step is visible: a step backwards can only keep a page longer, never close it
# early, and ``reconcile`` caps how far ahead a deadline may sit so a large step
# back cannot keep one for longer than one grant; a step forwards closes pages
# early by the size of the step, which the shell softens by closing only a record
# that two consecutive sweeps found due.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

# meiva:note
# The largest grant a deadline may be ahead of "now": the 24h renewal cap, or the
# configured default TTL when that is longer. Applied on every reconciliation.
MAX_GRANT_SECONDS = 86_400.0

LEDGER_VERSION = 1


# meiva:note
# Chrome's tabs as the ledger needs them, read fresh and never remembered.
#
# ``pages``: every open page target with its URL. ``agent_groups``: the agent
# each page target belongs to by its tab group's title (``Meiva #<id>``); a
# target missing from it is in no agent group.
@dataclass(frozen=True)
class TabState:
    pages: Mapping[str, str]
    agent_groups: Mapping[str, int]


# meiva:note
# A stack-created page: its owner, deadline (epoch seconds), whether Chrome has
# shown it in the owner's group (``grouped``) and the URL last seen (how a page
# is recognised after a Chrome restart).
@dataclass(frozen=True)
class PageRecord:
    owner: int
    expires_at: float
    grouped: bool
    url: str = ""


# meiva:note
# What the ledger file holds: the records and whether the one-time migration ran.
@dataclass(frozen=True)
class LedgerFile:
    records: dict[str, PageRecord]
    migrated: bool


# meiva:note
# A recorded page that is past its deadline (or is being released).
@dataclass(frozen=True)
class ExpiredPage:
    target_id: str
    owner: int
    url: str
    grouped: bool


# meiva:note
# The outcome of one reconciliation.
#
# ``records`` is what to persist; it still holds the ``due`` pages (the caller
# removes each one once Chrome no longer has it). ``adopted``: (target, agent)
# pairs taken in by the one-time migration. ``inherited``: (gone target, new
# target, owner) triples of a record passed to a restored tab. ``vanished``:
# records whose target Chrome no longer has and that were not inherited.
# ``taken``: records of pages that were in their owner's group and no longer
# are: the user took them. ``unrecorded``: pages in an agent group with no
# record that neither the migration nor an inheritance explained.
@dataclass(frozen=True)
class Reconciled:
    records: dict[str, PageRecord]
    adopted: tuple[tuple[str, int], ...]
    inherited: tuple[tuple[str, str, int], ...]
    vanished: tuple[str, ...]
    taken: tuple[str, ...]
    unrecorded: int
    due: tuple[ExpiredPage, ...]
    migrated: bool


def in_owner_group(record: PageRecord, target_id: str, tabs: TabState) -> bool:
    return tabs.agent_groups.get(target_id) == record.owner


# meiva:note
# new target -> gone target, for every (owner, URL) with exactly one of each.
def _inheritance(
    gone: Mapping[str, PageRecord], free: Mapping[str, int], tabs: TabState
) -> dict[str, str]:
    gone_by_key: dict[tuple[int, str], list[str]] = {}
    for target_id, record in gone.items():
        if record.grouped and record.url:
            gone_by_key.setdefault((record.owner, record.url), []).append(target_id)
    free_by_key: dict[tuple[int, str], list[str]] = {}
    for target_id, agent_id in free.items():
        free_by_key.setdefault((agent_id, tabs.pages[target_id]), []).append(target_id)
    passed: dict[str, str] = {}
    for key, old in gone_by_key.items():
        new = free_by_key.get(key, [])
        # meiva:note
        # The URL must be unique among ALL of Chrome's pages: a user's tab with the
        # same URL could be the one a layout restore put in the group.
        unique = sum(1 for url in tabs.pages.values() if url == key[1]) == 1
        if len(old) == 1 and len(new) == 1 and unique:
            passed[new[0]] = old[0]
    return passed


# meiva:note
# Bring the records in line with Chrome's tabs and say which pages are due.
# meiva:note
# The records of pages Chrome still has: a grouped record whose page left its
# owner's group is the user's now (returned in `taken`, no longer kept); a record
# first seen in its owner's group becomes grouped; deadlines are clamped to
# `ceiling` and the URL follows the page.
def _surviving(
    records: Mapping[str, PageRecord], tabs: TabState, ceiling: float
) -> tuple[dict[str, PageRecord], list[str]]:
    kept: dict[str, PageRecord] = {}
    taken: list[str] = []
    for target_id, record in records.items():
        if target_id not in tabs.pages:
            continue
        here = in_owner_group(record, target_id, tabs)
        if record.grouped and not here:
            taken.append(target_id)
            continue
        kept[target_id] = replace(
            record,
            grouped=record.grouped or here,
            expires_at=min(record.expires_at, ceiling),
            url=tabs.pages[target_id],
        )
    return kept, taken


def _inherited(
    passed: Mapping[str, str], gone: Mapping[str, PageRecord], tabs: TabState, ceiling: float
) -> dict[str, PageRecord]:
    return {
        new_id: replace(
            gone[old_id],
            grouped=True,
            expires_at=min(gone[old_id].expires_at, ceiling),
            url=tabs.pages[new_id],
        )
        for new_id, old_id in passed.items()
    }


def _migrated(
    leftover: Mapping[str, int], tabs: TabState, now: float, ttl: float, *, migrate: bool
) -> dict[str, PageRecord]:
    if not migrate:
        return {}
    return {
        t: PageRecord(agent_id, now + ttl, grouped=True, url=tabs.pages[t])
        for t, agent_id in leftover.items()
    }


def _due(records: Mapping[str, PageRecord], now: float) -> tuple[ExpiredPage, ...]:
    return tuple(
        ExpiredPage(t, r.owner, r.url, r.grouped) for t, r in records.items() if now >= r.expires_at
    )


def reconcile(
    records: Mapping[str, PageRecord],
    tabs: TabState,
    *,
    now: float,
    default_ttl_seconds: float,
    migrate: bool,
) -> Reconciled:
    ceiling = now + max(default_ttl_seconds, MAX_GRANT_SECONDS)
    gone = {t: r for t, r in records.items() if t not in tabs.pages}
    free = {t: a for t, a in tabs.agent_groups.items() if t in tabs.pages and t not in records}
    passed = _inheritance(gone, free, tabs)
    kept, taken = _surviving(records, tabs, ceiling)
    inherited = _inherited(passed, gone, tabs, ceiling)
    leftover = {t: a for t, a in free.items() if t not in passed}
    adopted = _migrated(leftover, tabs, now, default_ttl_seconds, migrate=migrate)
    everything = {**kept, **inherited, **adopted}
    return Reconciled(
        records=everything,
        adopted=tuple((t, r.owner) for t, r in adopted.items()),
        inherited=tuple((old, new, gone[old].owner) for new, old in passed.items()),
        vanished=tuple(t for t in gone if t not in set(passed.values())),
        taken=tuple(taken),
        unrecorded=0 if migrate else len(leftover),
        due=_due(everything, now),
        migrated=migrate,
    )


# meiva:note
# The last check before a close, on a fresh read of Chrome: the page is still
# there, the browser would keep another page, and a grouped page is still in its
# owner's group (a page the user took since the last reconciliation stays).
def may_close(record: PageRecord, target_id: str, tabs: TabState) -> bool:
    if target_id not in tabs.pages or len(tabs.pages) <= 1:
        return False
    return not record.grouped or in_owner_group(record, target_id, tabs)


# meiva:note
# Every page ``agent_id`` owns that Chrome still has.
def agent_pages(
    records: Mapping[str, PageRecord], tabs: TabState, agent_id: int
) -> list[ExpiredPage]:
    return [
        ExpiredPage(t, agent_id, r.url, r.grouped)
        for t, r in records.items()
        if r.owner == agent_id and t in tabs.pages
    ]


# meiva:note
# The one page of ``agent_id`` (recorded, still open) showing ``url``, else None.
#
# How a page number of a fresh chrome-devtools-mcp process is tied back to a
# target: only an unambiguous match counts.
def target_of_url(
    records: Mapping[str, PageRecord], tabs: TabState, agent_id: int, url: str
) -> str | None:
    matches = [t for t, r in records.items() if r.owner == agent_id and tabs.pages.get(t) == url]
    return matches[0] if len(matches) == 1 else None
