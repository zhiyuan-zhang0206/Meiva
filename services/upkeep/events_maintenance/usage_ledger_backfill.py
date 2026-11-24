# meiva:link base/lm/usage/README.md

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal, NamedTuple

import psycopg
from psycopg.rows import class_row

from base.agents.model.harness_models import resolve_harness_catalog_model
from base.api_contracts.agent_contract import harness_billing_of_source
from base.api_contracts.usage import UsageLedgerEntry
from base.db.rows import query_all
from base.db.usage_ledger import insert_usage
from base.lm.pricing import CacheTokens, PriceBook, quote

_SELECT = """
    SELECT event_uid, ts, agent_id, machine, cluster, attributes
    FROM telemetry_events
    WHERE event_name = 'llm_usage' AND category = 'telemetry'
      AND ts >= %(start)s AND ts < %(end)s
      AND (%(after_ts)s::timestamptz IS NULL OR (ts, event_uid) > (%(after_ts)s, %(after_uid)s))
    ORDER BY ts, event_uid
    LIMIT %(batch_size)s
"""
_RATE_KEYS = (
    "price_miss",
    "price_hit",
    "price_out",
    "price_write_5m",
    "price_write_1h",
    "price_write_30m",
)


@dataclass(frozen=True, slots=True)
class _Event:
    event_uid: int
    ts: datetime
    agent_id: int | None
    machine: str
    cluster: str
    attributes: dict[str, object]

    @property
    def call_id(self) -> str:
        return _text(self.attributes.get("call_id")) or (
            f"legacy:{self.event_uid}:{self.ts.astimezone(UTC).isoformat()}"
        )

    @property
    def occurred_at(self) -> datetime:
        raw = _text(self.attributes.get("occurred_at"))
        if raw is None:
            return self.ts
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return self.ts
        return parsed if parsed.utcoffset() is not None else self.ts


@dataclass(slots=True)
class BackfillCounts:
    scanned: int = 0
    inserted: int = 0
    catalog_priced: int = 0
    still_unpriced: int = 0


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _price(value: object) -> Decimal | None:
    if type(value) not in (int, float, str):
        return None
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        return None
    return price if price.is_finite() and price >= 0 else None


@dataclass(frozen=True, slots=True)
class _LegacyPrice:
    usd: Decimal | None
    source: str | None
    snapshot: dict[str, float | str]


def _historical_price(event: _Event, model: str, prices: PriceBook) -> _LegacyPrice:
    attrs = event.attributes
    stored = _price(attrs.get("cost_usd_exact"))
    if stored is None:
        stored = _price(attrs.get("cost_usd"))
    snapshot: dict[str, float | str] = {
        key: str(value) for key in _RATE_KEYS if (value := _price(attrs.get(key))) is not None
    }
    if stored is not None:
        return _LegacyPrice(stored, "legacy", snapshot)
    canonical = resolve_harness_catalog_model(model, prices)
    calculated = quote(
        canonical,
        _count(attrs.get("in_total")),
        _count(attrs.get("out_total")),
        CacheTokens(
            read=_count(attrs.get("cache_read")) or 0,
            write_5m=_count(attrs.get("cache_write_5m")) or 0,
            write_1h=_count(attrs.get("cache_write_1h")) or 0,
            write_30m=_count(attrs.get("cache_write_30m")) or 0,
        ),
        at=event.occurred_at,
        prices=prices,
    )
    if calculated is None or calculated.exact_usd is None:
        return _LegacyPrice(None, None, {})
    rates = calculated.rates
    values = (*rates.as_tuple(), rates.cache_write_5m, rates.cache_write_1h, rates.cache_write_30m)
    snapshot = {
        key: str(rate) for key, rate in zip(_RATE_KEYS, values, strict=True) if rate is not None
    }
    snapshot.update(priced_at=event.occurred_at.isoformat(), priced_model=canonical)
    return _LegacyPrice(calculated.exact_usd, "historical_catalog", snapshot)


def _entry(event: _Event, prices: PriceBook) -> UsageLedgerEntry:
    attrs = event.attributes
    model = _text(attrs.get("model")) or ""
    source = _text(attrs.get("source"))
    price = _historical_price(event, model, prices)
    tokens_in = _count(attrs.get("in_total"))
    tokens_out = _count(attrs.get("out_total"))
    return UsageLedgerEntry(
        call_id=event.call_id,
        occurred_at=event.occurred_at,
        agent_id=event.agent_id,
        machine=event.machine,
        cluster=event.cluster,
        source=source,
        billing=harness_billing_of_source(source).value,
        usage_kind=_text(attrs.get("usage_kind")) or "legacy",
        model=model,
        requested_model=_text(attrs.get("requested_model")),
        provider=prices.vendor(model),
        provider_request_id=_text(attrs.get("provider_request_id")),
        calls=_count(attrs.get("calls")) or 1,
        complete=attrs.get("complete") is not False
        and tokens_in is not None
        and tokens_out is not None,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_reasoning=_count(attrs.get("reasoning")),
        cache_read=_count(attrs.get("cache_read")),
        cache_write_5m=_count(attrs.get("cache_write_5m")),
        cache_write_1h=_count(attrs.get("cache_write_1h")),
        cache_write_30m=_count(attrs.get("cache_write_30m")),
        cost_usd=price.usd,
        price_snapshot=price.snapshot,
        price_source=price.source,
    )


def backfill_usage_ledger(
    conn: psycopg.Connection,
    *,
    start: datetime,
    end: datetime,
    batch_size: int,
    prices: PriceBook,
) -> BackfillCounts:
    if start.utcoffset() is None or end.utcoffset() is None or start >= end or batch_size <= 0:
        raise ValueError(
            "backfill requires an increasing aware date range and a positive batch size"
        )
    counts = BackfillCounts()
    after_ts: datetime | None = None
    after_uid: int | None = None
    while True:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ WRITE")
            conn.execute("SET LOCAL synchronous_commit TO on")
            events = _read_page(conn, start, end, batch_size, after_ts, after_uid)
            if not events:
                return counts
            _import_batch(conn, events, prices, counts)
        last = events[-1]
        after_ts, after_uid = last.ts, last.event_uid


def _read_page(
    conn: psycopg.Connection,
    start: datetime,
    end: datetime,
    batch_size: int,
    after_ts: datetime | None,
    after_uid: int | None,
) -> list[_Event]:
    with conn.cursor(row_factory=class_row(_Event)) as cur:
        cur.execute(
            _SELECT,
            {
                "start": start,
                "end": end,
                "after_ts": after_ts,
                "after_uid": after_uid,
                "batch_size": batch_size,
            },
        )
        return cur.fetchall()


@dataclass(frozen=True, slots=True)
class _ImportedId:
    call_id: str


def _import_batch(
    conn: psycopg.Connection, events: list[_Event], prices: PriceBook, counts: BackfillCounts
) -> None:
    with conn.cursor(row_factory=class_row(_ImportedId)) as cur:
        cur.execute(
            "SELECT call_id FROM llm_usage_ledger WHERE call_id = ANY(%s)",
            ([event.call_id for event in events],),
        )
        imported = {row.call_id for row in cur}
    for event in events:
        counts.scanned += 1
        if event.call_id in imported:
            continue
        entry = _entry(event, prices)
        if insert_usage(conn, entry, check_replay=False):
            counts.inserted += 1
            counts.catalog_priced += entry.price_source == "historical_catalog"
            counts.still_unpriced += entry.cost_usd is None


_LEDGER_TOTALS = """
    SELECT (occurred_at AT TIME ZONE 'UTC')::date AS day, model, billing,
           count(*) AS reports, sum(calls)::bigint AS calls,
           COALESCE(sum(tokens_in), 0)::bigint AS tokens_in,
           COALESCE(sum(tokens_out), 0)::bigint AS tokens_out,
           COALESCE(sum(cache_read), 0)::bigint AS cache_read,
           COALESCE(sum(cache_write_5m), 0)::bigint AS cache_write_5m,
           COALESCE(sum(cache_write_1h), 0)::bigint AS cache_write_1h,
           COALESCE(sum(cache_write_30m), 0)::bigint AS cache_write_30m,
           COALESCE(sum(tokens_reasoning), 0)::bigint AS tokens_reasoning,
           count(*) FILTER (WHERE NOT complete) AS incomplete_reports,
           COALESCE(sum(calls) FILTER (WHERE cost_usd IS NULL), 0)::bigint AS unpriced_calls,
           COALESCE(sum(cost_usd), 0) AS cost_usd,
           COALESCE(sum(cost_usd) FILTER (WHERE price_source = 'historical_catalog'), 0)
             AS historical_price_usd
    FROM llm_usage_ledger
    WHERE occurred_at >= %s AND occurred_at < %s
    GROUP BY 1, 2, 3
"""

_LEDGER_REPORTS = """
    SELECT call_id, occurred_at, agent_id, model, billing, calls, complete,
           tokens_in, tokens_out, tokens_reasoning, cache_read,
           NULLIF(cache_write_5m, 0) AS cache_write_5m,
           NULLIF(cache_write_1h, 0) AS cache_write_1h,
           NULLIF(cache_write_30m, 0) AS cache_write_30m,
           cost_usd, COALESCE(price_source = 'historical_catalog', false) AS catalog_priced
    FROM llm_usage_ledger
    WHERE occurred_at >= %s AND occurred_at < %s AND call_id = ANY(%s)
"""

_LEDGER_IDS = """
    SELECT call_id FROM llm_usage_ledger WHERE occurred_at >= %s AND occurred_at < %s
"""

_LIVE_WRITERS = """
    WITH live_reports AS (
        SELECT ledger.call_id, ledger.machine,
               CASE WHEN count(DISTINCT events.process) = 1 THEN min(events.process) END AS process
        FROM llm_usage_ledger AS ledger
        LEFT JOIN telemetry_events AS events
          ON events.attributes->>'call_id' = ledger.call_id
         AND events.machine = ledger.machine AND events.cluster = ledger.cluster
         AND events.ts >= %(start)s AND events.ts < %(end)s AND events.ts >= ledger.recorded_at
         AND events.category = 'telemetry' AND events.event_name = 'llm_usage'
         AND CASE
             WHEN pg_input_is_valid(events.attributes->>'occurred_at', 'timestamp with time zone')
             THEN (events.attributes->>'occurred_at')::timestamptz
             END = ledger.occurred_at
        WHERE ledger.occurred_at >= %(start)s AND ledger.occurred_at < %(end)s
          AND ledger.price_source = 'live'
        GROUP BY ledger.call_id, ledger.machine
    )
    SELECT machine, process, count(*) AS priced_reports
    FROM live_reports
    GROUP BY machine, process
    ORDER BY machine, process
"""


class _LedgerReport(NamedTuple):
    call_id: str
    occurred_at: datetime
    agent_id: int | None
    model: str
    billing: str
    calls: int
    complete: bool
    tokens_in: int | None
    tokens_out: int | None
    tokens_reasoning: int | None
    cache_read: int | None
    cache_write_5m: int | None
    cache_write_1h: int | None
    cache_write_30m: int | None
    cost_usd: Decimal | None
    catalog_priced: bool

    @classmethod
    def from_entry(cls, entry: UsageLedgerEntry) -> "_LedgerReport":
        return cls(
            entry.call_id,
            entry.occurred_at,
            entry.agent_id,
            entry.model,
            entry.billing,
            entry.calls,
            entry.complete,
            entry.tokens_in,
            entry.tokens_out,
            entry.tokens_reasoning,
            entry.cache_read,
            entry.cache_write_5m or None,
            entry.cache_write_1h or None,
            entry.cache_write_30m or None,
            entry.cost_usd,
            entry.price_source == "historical_catalog",
        )


@dataclass(slots=True, kw_only=True)
class UsageReconcileTotals:
    day: date
    model: str
    billing: str
    reports: int = 0
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_write_30m: int = 0
    tokens_reasoning: int = 0
    incomplete_reports: int = 0
    unpriced_calls: int = 0
    cost_usd: Decimal = Decimal(0)
    historical_price_usd: Decimal = Decimal(0)

    def include(self, entry: UsageLedgerEntry) -> None:
        self.reports += 1
        self.calls += entry.calls
        self.tokens_in += entry.tokens_in or 0
        self.tokens_out += entry.tokens_out or 0
        self.cache_read += entry.cache_read or 0
        self.cache_write_5m += entry.cache_write_5m or 0
        self.cache_write_1h += entry.cache_write_1h or 0
        self.cache_write_30m += entry.cache_write_30m or 0
        self.tokens_reasoning += entry.tokens_reasoning or 0
        self.incomplete_reports += not entry.complete
        if entry.cost_usd is None:
            self.unpriced_calls += entry.calls
        else:
            with localcontext(prec=80):
                self.cost_usd += entry.cost_usd
                if entry.price_source == "historical_catalog":
                    self.historical_price_usd += entry.cost_usd

    @property
    def key(self) -> tuple[date, str, str]:
        return self.day, self.model, self.billing


@dataclass(frozen=True, slots=True)
class LiveUsageWriter:
    machine: str
    process: str | None
    priced_reports: int


@dataclass(frozen=True, slots=True)
class UsageReconcileGroup:
    source: UsageReconcileTotals | None
    ledger: UsageReconcileTotals | None
    matches: bool


@dataclass(frozen=True, slots=True)
class UsageRecordDifference:
    call_id: str
    kind: Literal["source_only", "ledger_only", "different_fields"]
    fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UsageReconcileReport:
    source_events: int
    repeated_source_reports: int
    mismatched_groups: int
    groups: list[UsageReconcileGroup]
    live_writers: list[LiveUsageWriter]
    record_differences: list[UsageRecordDifference]

    @property
    def matches(self) -> bool:
        return self.mismatched_groups == 0 and not self.record_differences


@dataclass(slots=True)
class _SourceCheck:
    groups: dict[tuple[date, str, str], UsageReconcileTotals] = field(default_factory=dict)
    seen_ids: set[str] = field(default_factory=set)
    differences: list[UsageRecordDifference] = field(default_factory=list)
    source_events: int = 0
    repeats: int = 0

    def include(self, entry: UsageLedgerEntry, recorded: _LedgerReport | None) -> None:
        self.seen_ids.add(entry.call_id)
        day = entry.occurred_at.astimezone(UTC).date()
        key = (day, entry.model, entry.billing)
        if key not in self.groups:
            self.groups[key] = UsageReconcileTotals(
                day=day, model=entry.model, billing=entry.billing
            )
        self.groups[key].include(entry)
        if recorded is None:
            self.differences.append(UsageRecordDifference(entry.call_id, "source_only"))
            return
        source = _LedgerReport.from_entry(entry)
        if source != recorded:
            changed = tuple(
                name
                for name, expected, actual in zip(source._fields, source, recorded, strict=True)
                if expected != actual
            )
            self.differences.append(
                UsageRecordDifference(entry.call_id, "different_fields", changed)
            )


def _reconcile_source(
    conn: psycopg.Connection,
    start: datetime,
    end: datetime,
    batch_size: int,
    prices: PriceBook,
) -> _SourceCheck:
    result = _SourceCheck()
    after_ts: datetime | None = None
    after_uid: int | None = None
    while events := _read_page(conn, start, end, batch_size, after_ts, after_uid):
        result.source_events += len(events)
        recorded = {
            row.call_id: row
            for row in query_all(
                conn,
                _LedgerReport,
                _LEDGER_REPORTS,
                (start, end, [event.call_id for event in events]),
            )
        }
        for event in events:
            if event.call_id in result.seen_ids:
                result.repeats += 1
                continue
            result.include(_entry(event, prices), recorded.get(event.call_id))
        after_ts, after_uid = events[-1].ts, events[-1].event_uid
    return result


def _include_ledger_only(
    conn: psycopg.Connection, start: datetime, end: datetime, batch_size: int, source: _SourceCheck
) -> None:
    with conn.cursor(name="usage_reconcile_ids", row_factory=class_row(_ImportedId)) as cur:
        cur.itersize = batch_size
        cur.execute(_LEDGER_IDS, (start, end))
        for row in cur:
            if row.call_id not in source.seen_ids:
                source.differences.append(UsageRecordDifference(row.call_id, "ledger_only"))


def reconcile_usage_ledger(
    conn: psycopg.Connection,
    *,
    start: datetime,
    end: datetime,
    batch_size: int,
    prices: PriceBook,
) -> UsageReconcileReport:
    if start.utcoffset() is None or end.utcoffset() is None or start >= end or batch_size <= 0:
        raise ValueError(
            "reconciliation requires an increasing aware date range and a positive batch size"
        )
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        source_check = _reconcile_source(conn, start, end, batch_size, prices)
        _include_ledger_only(conn, start, end, batch_size, source_check)
        ledger = {
            row.key: row
            for row in query_all(conn, UsageReconcileTotals, _LEDGER_TOTALS, (start, end))
        }
        live_writers = query_all(conn, LiveUsageWriter, _LIVE_WRITERS, {"start": start, "end": end})
    sources = source_check.groups
    groups: list[UsageReconcileGroup] = []
    for key in sorted(sources.keys() | ledger.keys()):
        source, recorded = sources.get(key), ledger.get(key)
        groups.append(UsageReconcileGroup(source, recorded, source == recorded))
    return UsageReconcileReport(
        source_check.source_events,
        source_check.repeats,
        sum(not group.matches for group in groups),
        groups,
        live_writers,
        sorted(source_check.differences, key=lambda row: row.call_id),
    )
