# meiva:note
# `events_window.py` holds the decisions of reading `/api/events` over a time window, as values only: the window itself, the week label of a day, and the paging of one read. Every request is offset 0 with a fixed page limit, because Loki offset paging is both quadratic and timeout-prone (the gateway's 60 s Loki budget, observed 2026-08-14); a slice whose page overflows (`meta.has_more`) is bisected in time until each half fits one page. Requests are count-free by design: `meta.total` is opt-in (`with_total=1`) and its count aggregation is exactly the Loki load the gateway shed (2026-08-18), so the plan reads `has_more` only, and a missing `has_more` raises `KeyError` instead of silently truncating. The API window is inclusive on both ends, so rows are deduplicated by surrogate row id across slice boundaries and sorted by `ts` (the API returns newest-first). `collect.py` owns the HTTP and the retry budget; `http_failure_detail` names the responder when that budget is spent.
# meiva:link meiva_builtins/skills/platform/meiva-self-evolution/docs/collection.meiva.okf.md
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

PAGE = 1000
MIN_SLICE = timedelta(seconds=1)
DIAG_FIELD_CHARS = 300


# meiva:note
# The half-open collection window `[start, end)`. An empty or inverted window is refused at construction.
@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise ValueError(f"empty window: [{self.start}, {self.end})")


# meiva:note
# The window a collection run covers: it ends at `to` (default `now`) and starts at `from_` (default `days` before its end).
def resolve_window(now: datetime, days: int, from_: datetime | None, to: datetime | None) -> Window:
    end = to or now
    return Window(from_ or (end - timedelta(days=days)), end)


# meiva:note
# The dataset label of a day: its ISO date in UTC. Each manual run gets a distinct file, and the schedule fires on Monday at 9 AM so its runs are dated naturally.
def week_label(now: datetime) -> str:
    return now.astimezone(UTC).date().isoformat()


# meiva:note
# The query parameters of one `/api/events` page request: offset is always 0, the limit is the page cap.
def page_params(
    category: str, from_: datetime, to: datetime, agent_id: int | None
) -> dict[str, str | int]:
    params: dict[str, str | int] = {
        "category": category,
        "from": from_.isoformat(),
        "to": to.isoformat(),
        "limit": PAGE,
        "offset": 0,
    }
    if agent_id is not None:
        params["agent_id"] = agent_id
    return params


# meiva:note
# The slice's rows are final: `truncated` is true when the slice is already as narrow as `MIN_SLICE` and still overflowed its page, so its tail may be missing.
class Take(NamedTuple):
    rows: list[Mapping[str, object]]
    truncated: bool


# meiva:note
# The slice overflowed its page and is wider than `MIN_SLICE`: read `[start, mid)` and `[mid, end)` instead.
class Split(NamedTuple):
    mid: datetime


# meiva:note
# What to do with the page read for the slice `[start, end]`. An empty page that says nothing more is waiting is an empty slice; a page that fits is taken as it is; an overflowing page is split in half unless the slice is already minimal.
def slice_step(
    batch: Sequence[Mapping[str, object]],
    meta: Mapping[str, object],
    start: datetime,
    end: datetime,
) -> Take | Split:
    has_more = meta["has_more"]
    if not batch and not has_more:
        return Take([], False)
    if not has_more or end - start <= MIN_SLICE:
        return Take(list(batch), bool(has_more))
    return Split(start + (end - start) / 2)


# meiva:note
# The window's rows from its slices in the order they were read: a row id seen before is dropped, rows without an id are kept, and the result is sorted by `ts`, oldest first.
def merge_slices(slices: Sequence[Sequence[Mapping[str, object]]]) -> list[Mapping[str, object]]:
    out: list[Mapping[str, object]] = []
    seen: set[object] = set()
    for rows in slices:
        for row in rows:
            row_id = row.get("id")
            if row_id is not None:
                if row_id in seen:
                    continue
                seen.add(row_id)
            out.append(row)
    out.sort(key=lambda row: str(row.get("ts", "")))
    return out


def _bounded(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= DIAG_FIELD_CHARS:
        return text
    return f"{text[:DIAG_FIELD_CHARS]}... (+{len(text) - DIAG_FIELD_CHARS} chars)"


# meiva:note
# The one-line evidence that identifies the responder of an HTTP failure after the retry budget is spent: the status line plus bounded header and body excerpts, because a local forward proxy's 502 and the gateway's own 503 are otherwise indistinguishable.
def http_failure_detail(status: int, reason: str, headers: Mapping[str, str], body: str) -> str:
    return (
        f"HTTP {status} {reason}; headers: {_bounded(str(dict(headers)))}; body: {_bounded(body)}"
    )
