# meiva:link base/lm/usage/README.md

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import psycopg
from psycopg.rows import class_row

from base.agents.model.harness_models import resolve_harness_catalog_model
from base.api_contracts.agent_contract import harness_billing_of_source
from base.api_contracts.usage import UsageLedgerEntry
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
                events = cur.fetchall()
            if not events:
                return counts
            _import_batch(conn, events, prices, counts)
        last = events[-1]
        after_ts, after_uid = last.ts, last.event_uid


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
