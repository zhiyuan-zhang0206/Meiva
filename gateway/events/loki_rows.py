# meiva:link gateway/events/docs/event-reads.md


from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from base.events.contract import EventGroupBy
from base.events.reads import EventRecord
from base.telemetry import event_id
from gateway.events import loki_query
from gateway.events.loki_query import LokiFilters
from gateway.http.config import GatewayAppState

__all__ = [
    "LokiAppState",
    "build_client",
    "count_event_groups",
    "count_events",
    "query_events",
]

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_PAD_NS: Final = 500
_NS_PER_MS: Final = 1_000_000


class LokiAppState(GatewayAppState):
    loki_client: httpx.Client


def build_client(base_url: str, *, timeout_seconds: float) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        timeout=timeout_seconds,
        trust_env=False,
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=4),
    )


def _ns(moment: datetime) -> int:
    delta = moment.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 10**9 + delta.microseconds * 1_000


class _Line(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ts: datetime
    trace_id: str | None
    span_id: str | None
    agent_id: int | None
    machine: str
    process: str
    category: str
    event_name: str
    level: str
    source: str
    target_agent_id: int | None
    attributes: dict[str, object]


class _Stream(BaseModel):
    model_config = ConfigDict(extra="ignore")

    values: list[tuple[str, str]]


class _LogAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    class Data(BaseModel):
        model_config = ConfigDict(extra="ignore")

        result: list[_Stream]

    status: Literal["success"]
    data: Data


class _Sample(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metric: dict[str, str]
    value: tuple[float, str]


class _CountAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    class Data(BaseModel):
        model_config = ConfigDict(extra="ignore")

        result: list[_Sample]

    status: Literal["success"]
    data: Data


def _record(stamp: str, line: str) -> EventRecord:
    body = _Line.model_validate_json(line)
    return {
        "id": event_id(line, int(stamp)),
        "line_sha256": sha256(line.encode()).hexdigest(),
        "ts": body.ts.astimezone(UTC),
        "trace_id": body.trace_id,
        "span_id": body.span_id,
        "agent_id": body.agent_id,
        "machine": body.machine,
        "process": body.process,
        "category": body.category,
        "event_name": body.event_name,
        "level": body.level,
        "source": body.source,
        "target_agent_id": body.target_agent_id,
        "attributes": body.attributes,
    }


def _get(client: httpx.Client, path: str, params: dict[str, str]) -> bytes:
    response = client.get(path, params=params)
    response.raise_for_status()
    return response.content


def query_events(
    client: httpx.Client,
    filters: LokiFilters,
    *,
    from_: datetime,
    to: datetime | None,
    now: datetime,
    limit: int,
) -> tuple[list[EventRecord], bool]:
    query = loki_query.log_query(filters)
    if query is None:
        return [], False
    end = _ns(now if to is None else to)
    content = _get(
        client,
        "/loki/api/v1/query_range",
        {
            "query": query,
            "start": str(_ns(from_) - _PAD_NS),
            "end": str(end + _PAD_NS + 1),
            "limit": str(limit + 1),
            "direction": "backward",
        },
    )
    answer = _LogAnswer.model_validate_json(content)
    rows = [_record(stamp, line) for stream in answer.data.result for stamp, line in stream.values]
    upper = now if to is None else to
    kept = [row for row in rows if from_ <= row["ts"] <= upper]
    kept.sort(key=lambda row: (row["ts"], row["id"]), reverse=True)
    return kept[:limit], len(kept) > limit


def _range_ms(from_: datetime, to_ns: int) -> int:
    return max(1, -(-(to_ns - _ns(from_)) // _NS_PER_MS))


def _vector(client: httpx.Client, query: str, *, at_ns: int) -> list[tuple[dict[str, str], int]]:
    content = _get(client, "/loki/api/v1/query", {"query": query, "time": str(at_ns)})
    answer = _CountAnswer.model_validate_json(content)
    return [(sample.metric, int(float(sample.value[1]))) for sample in answer.data.result]


def count_events(
    client: httpx.Client,
    filters: LokiFilters,
    *,
    from_: datetime,
    to: datetime | None,
    now: datetime,
) -> int:
    at_ns = _ns(now if to is None else to)
    query = loki_query.count_query(filters, by=None, range_ms=_range_ms(from_, at_ns))
    if query is None:
        return 0
    return sum(count for _, count in _vector(client, query, at_ns=at_ns))


def count_event_groups(
    client: httpx.Client,
    filters: LokiFilters,
    *,
    by: EventGroupBy,
    from_: datetime,
    to: datetime | None,
    now: datetime,
    limit: int,
) -> list[tuple[str | None, int]]:
    at_ns = _ns(now if to is None else to)
    query = loki_query.count_query(filters, by=by, range_ms=_range_ms(from_, at_ns))
    if query is None:
        return []
    label = loki_query.metric_group_label(by)
    groups = [
        (metric.get(label) or None, count)
        for metric, count in _vector(client, query, at_ns=at_ns)
        if count > 0
    ]
    groups.sort(key=lambda group: (-group[1], group[0] is None, group[0] or ""))
    return groups[:limit]
