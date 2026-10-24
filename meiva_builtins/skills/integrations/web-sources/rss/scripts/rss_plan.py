# meiva:note
# Which entries of a feed an rss run takes and builds, over values: the `--since` cutoff, the
# date window and newest-first order, and the sync plan against what the raw mirror already
# holds. `feed.py` reads the clock and the mirror and executes the plan.
# meiva:link meiva_builtins/skills/integrations/web-sources/docs/web-sources.meiva.okf.md

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Sequence
from dataclasses import dataclass

from rss_post import Entry, Post


# meiva:note
# A `--since` value as a unix-seconds cutoff: `30d` / `12h` (that long before `now`) or an ISO
# date `2026-05-07` (UTC unless it carries an offset). None means no cutoff.
def since_to_ts(since: str | None, now: float) -> float | None:
    if since is None:
        return None
    m = re.fullmatch(r"(\d+)([dh])", since)
    if m:
        secs = int(m.group(1)) * (86400 if m.group(2) == "d" else 3600)
        return now - secs
    parsed = _dt.datetime.fromisoformat(since)
    parsed = parsed.replace(tzinfo=_dt.UTC) if parsed.tzinfo is None else parsed.astimezone(_dt.UTC)
    return parsed.timestamp()


# meiva:note
# The entries inside the date window, newest first.
#
# `since_ts` drops entries older than the cutoff; an entry with no date is kept, since a missing
# date is not evidence of being old. Feeds are conventionally reverse-chron but not guaranteed,
# so the result is sorted by date, and undated entries sort as `now` so they stay at the top in
# feed order (the sort is stable and they all share one key).
def select_entries(
    entries: Sequence[Entry], *, since_ts: float | None, now: float
) -> tuple[Entry, ...]:
    kept = [
        e
        for e in entries
        if since_ts is None or e.published_ts is None or e.published_ts >= since_ts
    ]
    return tuple(
        sorted(kept, key=lambda e: now if e.published_ts is None else e.published_ts, reverse=True)
    )


# meiva:note
# True if a saved post holds everything this run would produce, so a re-run reuses it. A `--full`
# run needs the saved post to actually carry a full body, not just the feed's summary.
def is_complete(post: Post, *, full: bool) -> bool:
    return not full or bool(post.get("_full"))


# meiva:note
# One entry of the sync plan: `build` it (and save the post under `key`, its id), or reuse the
# post already in the mirror.
@dataclass(frozen=True)
class SyncStep:
    entry: Entry
    key: str
    build: bool


# meiva:note
# The steps of a sync in output order, and the entries that have no id, link or title to key a
# mirror directory by (they are reported and left out).
@dataclass(frozen=True)
class SyncPlan:
    steps: tuple[SyncStep, ...]
    unkeyed: tuple[Entry, ...]


# meiva:note
# What a sync does with the selected entries, given the ids whose saved post is `complete`.
# Complete entries are reused. Every other entry is built, until `limit` newly built entries
# have been planned; that entry and the ones after it wait for the next run, so a re-run
# advances through the backlog instead of re-examining the same leading N. An id seen twice in
# one feed is built once and then reused.
def plan_sync(selected: Sequence[Entry], complete: frozenset[str], limit: int | None) -> SyncPlan:
    steps: list[SyncStep] = []
    unkeyed: list[Entry] = []
    built: set[str] = set()
    for entry in selected:
        key = entry.id
        if key is None:
            unkeyed.append(entry)
        elif key in complete or key in built:
            steps.append(SyncStep(entry, key, build=False))
        elif limit is not None and len(built) >= limit:
            break
        else:
            built.add(key)
            steps.append(SyncStep(entry, key, build=True))
    return SyncPlan(steps=tuple(steps), unkeyed=tuple(unkeyed))
