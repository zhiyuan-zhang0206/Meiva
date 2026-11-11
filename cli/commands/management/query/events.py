# meiva:note
# `meiva events ls` and `meiva events count` — the unified event stream (audit, telemetry and
# log) read through `GET /api/events` and `GET /api/events/counts`.
#
# A thin client: every filter is forwarded as the gateway names it, and the window and page size
# are the gateway's own defaults (the last 24 hours, the configured page) unless the operator
# gives `--hours` / `--from` / `--limit`. The gateway refuses an unknown category, level or a
# timestamp without an offset; the verb prints its reason and exits 1.

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, JsonValue

from base.cluster.machine import GatewayAccess
from base.events.contract import EventGroupBy
from cli.boundary.gateway_read import fetch, print_json, render_table

_ATTRIBUTES_WIDTH = 100


# meiva:note
# The filters `meiva events ls` forwards. `None` means the operator did not set it.
@dataclass(frozen=True)
class EventsFilter:
    category: str | None = None
    name: str | None = None
    agent_id: int | None = None
    trace_id: str | None = None
    machine: str | None = None
    level: str | None = None
    tier: str | None = None
    since: str | None = None
    until: str | None = None
    hours: float | None = None
    limit: int | None = None
    offset: int | None = None
    with_total: bool = False


# meiva:note
# The query parameters the gateway names for these filters. The window and the filters are the
# same for the list and the counts; the page (`limit`, `offset`, `with_total`) is the list's.
def _filter_params(flt: EventsFilter) -> dict[str, str | int | float | bool | None]:
    return {
        "category": flt.category,
        "event_name": flt.name,
        "agent_id": flt.agent_id,
        "trace_id": flt.trace_id,
        "machine": flt.machine,
        "level": flt.level,
        "tier": flt.tier,
        "from": flt.since,
        "to": flt.until,
        "hours": flt.hours,
        "limit": flt.limit,
    }


# meiva:note
# The columns the table shows; `--json` prints the whole response instead.
class _EventItem(BaseModel):
    ts: datetime
    agent_id: int | None
    machine: str
    category: str
    event_name: str
    level: str
    source: str
    attributes: dict[str, JsonValue]


class _EventsMeta(BaseModel):
    total: int | None
    window_from: datetime | None
    offset: int
    has_more: bool


class _EventsPage(BaseModel):
    meta: _EventsMeta
    items: list[_EventItem]


# meiva:note
# The attributes as compact JSON, cut to one table line.
def _attributes_cell(attributes: dict[str, JsonValue]) -> str:
    if not attributes:
        return ""
    text = json.dumps(attributes, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= _ATTRIBUTES_WIDTH else text[: _ATTRIBUTES_WIDTH - 3] + "..."


# meiva:note
# Print one page of events, newest first.
def cmd_events_ls(access: GatewayAccess, flt: EventsFilter, *, as_json: bool) -> int:
    resp = fetch(
        access,
        "/api/events",
        {
            **_filter_params(flt),
            "offset": flt.offset,
            "with_total": True if flt.with_total else None,
        },
    )
    if resp is None:
        return 1
    if as_json:
        print_json(resp)
        return 0
    page = _EventsPage.model_validate_json(resp.content)
    since = page.meta.window_from.isoformat() if page.meta.window_from else "(unbounded)"
    if not page.items:
        print(f"(no events since {since})")
        return 0
    print(
        render_table(
            ("ts", "level", "agent", "machine", "category", "event", "source", "attributes"),
            [
                (
                    item.ts.isoformat(timespec="seconds"),
                    item.level,
                    "" if item.agent_id is None else str(item.agent_id),
                    item.machine,
                    item.category,
                    item.event_name,
                    item.source,
                    _attributes_cell(item.attributes),
                )
                for item in page.items
            ],
        )
    )
    total = f" of {page.meta.total}" if page.meta.total is not None else ""
    print(f"({len(page.items)}{total} events since {since})")
    if page.meta.has_more:
        print(f"(more events: repeat with --offset {page.meta.offset + len(page.items)})")
    return 0


class _CountGroup(BaseModel):
    key: str | None
    count: int


class _CountsMeta(BaseModel):
    window_from: datetime | None
    total: int
    truncated: bool


class _CountsPage(BaseModel):
    meta: _CountsMeta
    groups: list[_CountGroup]


# meiva:note
# Print the events of the window counted per value of one dimension, largest group first. The
# counting happens in the gateway's SQL; this verb prints the groups it returns.
def cmd_events_count(
    access: GatewayAccess, flt: EventsFilter, *, by: EventGroupBy, as_json: bool
) -> int:
    resp = fetch(access, "/api/events/counts", {"by": by, **_filter_params(flt)})
    if resp is None:
        return 1
    if as_json:
        print_json(resp)
        return 0
    page = _CountsPage.model_validate_json(resp.content)
    since = page.meta.window_from.isoformat() if page.meta.window_from else "(unbounded)"
    if not page.groups:
        print(f"(no events since {since})")
        return 0
    print(
        render_table(
            ("count", by),
            [
                (str(group.count), "(none)" if group.key is None else group.key)
                for group in page.groups
            ],
        )
    )
    print(f"({page.meta.total} events since {since})")
    if page.meta.truncated:
        print("(more groups: raise --limit)")
    return 0
