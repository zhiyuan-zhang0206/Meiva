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
# The one rule that matters: a page may be closed only on positive evidence that
# it is ours, namely that Chrome shows it inside a ``Meiva #<id>`` tab group right
# now. A record alone never closes a page (the user may have moved the tab), and a
# page in no such group is never touched, whatever any record says.
#
# Deadlines are wall-clock epoch seconds. A monotonic clock does not survive the
# process, and this ledger exists to outlive it. The price is that a wall-clock
# step is visible: a step backwards can only keep a page longer, never close it
# early, and ``reconcile`` caps how far ahead a deadline may sit so a large step
# back cannot keep one for longer than one grant; a step forwards closes pages
# early by the size of the step.


from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

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
# target missing from it is in no agent group (the user's own tab, or one of
# ours the grouping never reached). The group title is Chrome's own persistent
# record of who made a page.
@dataclass(frozen=True)
class TabState:
    pages: Mapping[str, str]
    agent_groups: Mapping[str, int]


# meiva:note
# A stack-created page: the agent that owns it and its deadline (epoch seconds).
@dataclass(frozen=True)
class PageRecord:
    owner: int
    expires_at: float


# meiva:note
# A page past its deadline that Chrome shows in its owner's tab group.
@dataclass(frozen=True)
class ExpiredPage:
    target_id: str
    owner: int
    url: str


# meiva:note
# The outcome of one reconciliation.
#
# ``records`` is what to persist: it still holds the ``to_close`` pages (the
# caller removes each one once it is closed, so a failed close is retried on
# the next pass). ``adopted``: (target, agent) pairs taken into the ledger from
# their tab group. ``vanished``: records whose target Chrome no longer has.
# ``lapsed``: expired records whose page is in no agent group -- dropped and
# never closed.
@dataclass(frozen=True)
class Reconciled:
    records: dict[str, PageRecord]
    adopted: tuple[tuple[str, int], ...]
    vanished: tuple[str, ...]
    lapsed: tuple[str, ...]
    to_close: tuple[ExpiredPage, ...]


# meiva:note
# Bring the records in line with Chrome's tabs and say which pages are due.
#
# - A record whose target Chrome no longer has is dropped.
# - A target in an agent group with no record is adopted: that agent owns it
#   and its deadline is ``now + default_ttl_seconds``. The group title is the
#   owner of the fact, so a record naming another agent follows the group.
# - A deadline beyond ``now + max(default_ttl, 24h)`` is pulled back to it.
# - A record past its deadline is returned in ``to_close`` when its page is in
#   an agent group, and dropped as ``lapsed`` when it is not.
# - A page that is in no agent group and has no record is not looked at.
def reconcile(
    records: Mapping[str, PageRecord],
    tabs: TabState,
    *,
    now: float,
    default_ttl_seconds: float,
) -> Reconciled:
    ceiling = now + max(default_ttl_seconds, MAX_GRANT_SECONDS)
    kept: dict[str, PageRecord] = {}
    vanished: list[str] = []
    for target_id, record in records.items():
        if target_id not in tabs.pages:
            vanished.append(target_id)
            continue
        owner = tabs.agent_groups.get(target_id, record.owner)
        kept[target_id] = PageRecord(owner, min(record.expires_at, ceiling))
    adopted: list[tuple[str, int]] = []
    for target_id, agent_id in tabs.agent_groups.items():
        if target_id in tabs.pages and target_id not in kept:
            kept[target_id] = PageRecord(agent_id, now + default_ttl_seconds)
            adopted.append((target_id, agent_id))
    due = [t for t, r in kept.items() if now >= r.expires_at]
    lapsed = [t for t in due if t not in tabs.agent_groups]
    for target_id in lapsed:
        del kept[target_id]
    to_close = [
        ExpiredPage(t, kept[t].owner, tabs.pages[t]) for t in closable(due, tabs) if t in kept
    ]
    return Reconciled(
        records=kept,
        adopted=tuple(adopted),
        vanished=tuple(vanished),
        lapsed=tuple(lapsed),
        to_close=tuple(to_close),
    )


# meiva:note
# The candidates that are safe to close: in an agent group, and never every page.
#
# Closing the last page target of a browser can end the browser (and
# chrome-devtools-mcp itself refuses to close its last page), so one page of
# the browser always stays; the held-back one is retried on a later pass.
def closable(candidates: list[str], tabs: TabState) -> list[str]:
    chosen = [t for t in candidates if t in tabs.pages and t in tabs.agent_groups]
    if chosen and set(chosen) >= set(tabs.pages):
        chosen.remove(chosen[-1])
    return chosen


# meiva:note
# Every page ``agent_id`` owns that Chrome shows in its own group, closable now.
def agent_pages(
    records: Mapping[str, PageRecord], tabs: TabState, agent_id: int
) -> list[ExpiredPage]:
    mine = [
        t
        for t, r in records.items()
        if r.owner == agent_id and tabs.agent_groups.get(t) == agent_id
    ]
    return [ExpiredPage(t, agent_id, tabs.pages[t]) for t in closable(mine, tabs)]


# meiva:note
# The one page of ``agent_id`` (recorded, in its group) showing ``url``, else None.
#
# How a page number of a fresh chrome-devtools-mcp process is tied back to a
# target: only an unambiguous match counts.
def target_of_url(
    records: Mapping[str, PageRecord], tabs: TabState, agent_id: int, url: str
) -> str | None:
    matches = [
        t
        for t, r in records.items()
        if r.owner == agent_id and tabs.agent_groups.get(t) == agent_id and tabs.pages.get(t) == url
    ]
    return matches[0] if len(matches) == 1 else None


def dump_records(records: Mapping[str, PageRecord]) -> str:
    payload = {
        "version": LEDGER_VERSION,
        "pages": {
            target_id: {"owner": record.owner, "expires_at": record.expires_at}
            for target_id, record in sorted(records.items())
        },
    }
    return json.dumps(payload, indent=2) + "\n"


# meiva:note
# The records a ledger file holds. Raises ValueError for anything not exactly
# the shape ``dump_records`` writes: a partly trusted ledger is worse than none.
def parse_records(text: str) -> dict[str, PageRecord]:
    raw = object_of(json.loads, text)
    if not is_object_dict(raw) or raw.get("version") != LEDGER_VERSION:
        raise ValueError("not a version-1 page ledger")
    pages = raw.get("pages")
    if not is_object_dict(pages):
        raise ValueError("'pages' must be an object")
    records: dict[str, PageRecord] = {}
    for target_id, entry in pages.items():
        if not target_id or not is_object_dict(entry):
            raise ValueError(f"malformed ledger entry for {target_id!r}")
        owner, expires_at = entry.get("owner"), entry.get("expires_at")
        if isinstance(owner, bool) or not isinstance(owner, int) or owner < 0:
            raise ValueError(f"malformed owner for {target_id!r}")
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, int | float)
            or not math.isfinite(expires_at)
        ):
            raise ValueError(f"malformed expires_at for {target_id!r}")
        records[target_id] = PageRecord(owner, float(expires_at))
    return records
