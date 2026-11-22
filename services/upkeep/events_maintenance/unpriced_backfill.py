from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from base.agents.model.harness_models import resolve_harness_catalog_model
from base.api_contracts.agent_contract import SUBSCRIPTION_HARNESSES
from base.db.rows import query_all, query_one
from base.events.contract import LLM_USAGE_KEYS
from base.lm.pricing import CacheTokens, PriceBook, quote
from base.lm.pricing.archive import load_archive
from base.telemetry.event_store import event_uid
from base.telemetry.metrics.event_sql import numeric
from services.upkeep.events_maintenance.token_totals import folded_through, rebuild_totals

_OBS_UPDATE_CHUNK_SIZE = 1000
_TELEMETRY_LOOKUP_CHUNK_SIZE = 1000

_COST = LLM_USAGE_KEYS["cost_usd"]


def load_price_book() -> PriceBook:
    return PriceBook(load_archive(), {})


@dataclass(frozen=True)
class UnpricedBackfillModelSummary:
    raw_model: str
    canonical_model: str
    observations_priced: int
    observations_still_unpriced: int
    added_cost_usd: Decimal


@dataclass(frozen=True)
class UnpricedBackfillReport:
    dry_run: bool
    observations_scanned: int
    observations_priced: int
    observations_still_unpriced: int
    metric_days_recomputed: int
    ledger_days_scanned: int
    ledger_days_updated: int
    ledger_calls_priced: int
    totals_rebuilt: bool
    added_observation_cost_usd: Decimal
    added_observation_cost_usd_subscription: Decimal
    by_model: tuple[UnpricedBackfillModelSummary, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "observations_scanned": self.observations_scanned,
            "observations_priced": self.observations_priced,
            "observations_still_unpriced": self.observations_still_unpriced,
            "metric_days_recomputed": self.metric_days_recomputed,
            "ledger_days_scanned": self.ledger_days_scanned,
            "ledger_days_updated": self.ledger_days_updated,
            "ledger_calls_priced": self.ledger_calls_priced,
            "totals_rebuilt": self.totals_rebuilt,
            "added_observation_cost_usd": float(self.added_observation_cost_usd),
            "added_observation_cost_usd_subscription": float(
                self.added_observation_cost_usd_subscription
            ),
            "by_model": [
                {
                    "raw_model": item.raw_model,
                    "canonical_model": item.canonical_model,
                    "observations_priced": item.observations_priced,
                    "observations_still_unpriced": item.observations_still_unpriced,
                    "added_cost_usd": float(item.added_cost_usd),
                }
                for item in self.by_model
            ],
        }


@dataclass(frozen=True)
class _UnpricedObsRow:
    event_id: Decimal
    agent_id: int
    occurred_at: datetime
    model: str | None
    usage_calls: int
    unpriced_calls: int
    unpriced_calls_subscription: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int


@dataclass(frozen=True)
class _CacheWriteRow:
    event_uid: int
    ts: datetime
    write_5m: int
    write_1h: int
    write_30m: int


@dataclass(frozen=True)
class _AffectedDayRow:
    agent_id: int
    day: date


@dataclass(frozen=True)
class _DayRow:
    day: date


@dataclass(frozen=True)
class _ExistsRow:
    present: bool


@dataclass(frozen=True)
class _PricedLedgerRow:
    agent_id: int
    model: str
    llm_calls: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    tokens_reasoning: int
    cost_usd: float
    cost_usd_subscription: float


@dataclass(frozen=True)
class _UnpricedTelemetryRow:
    agent_id: int
    ts: datetime
    model: str
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    cache_write_5m: int
    cache_write_1h: int
    cache_write_30m: int
    tokens_reasoning: int
    is_subscription: bool


@dataclass(frozen=True)
class _ExistingLedgerRow:
    agent_id: int
    model: str
    llm_calls: int
    estimated_calls: int


@dataclass(frozen=True)
class _UnpricedCountRow:
    total: int


@dataclass
class _ModelStat:
    priced: int = 0
    still_unpriced: int = 0
    added_cost_usd: Decimal = Decimal(0)


@dataclass
class _LedgerBucket:
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    tokens_reasoning: int = 0
    cost_usd: float = 0.0
    cost_usd_subscription: float = 0.0
    costed_calls: int = 0
    unpriced_calls: int = 0
    unpriced_calls_subscription: int = 0


_UNPRICED_OBS_SQL = """
    SELECT event_id, agent_id, occurred_at, model, usage_calls,
           unpriced_calls, unpriced_calls_subscription,
           tokens_in, tokens_out, tokens_cached
    FROM agent_metric_observations
    WHERE kind = 'usage'
      AND unpriced_calls > 0
      AND cost_usd = 0
    ORDER BY occurred_at, event_id
"""

_CACHE_WRITE_LOOKUP_SQL = f"""
    SELECT t.event_uid, t.ts,
           COALESCE({numeric(LLM_USAGE_KEYS["cache_write_5m"])}, 0)::bigint AS write_5m,
           COALESCE({numeric(LLM_USAGE_KEYS["cache_write_1h"])}, 0)::bigint AS write_1h,
           COALESCE({numeric(LLM_USAGE_KEYS["cache_write_30m"])}, 0)::bigint AS write_30m
    FROM telemetry_events t
    JOIN unnest(%s::bigint[], %s::timestamptz[]) AS k(event_uid, ts)
      ON t.event_uid = k.event_uid AND t.ts = k.ts
    WHERE t.event_name = 'llm_usage'
"""  # noqa: S608 — keys come from registered payload constants

_UPDATE_OBS_SQL = """
    UPDATE agent_metric_observations o
    SET model = u.model,
        cost_usd = u.cost_usd,
        cost_usd_subscription = u.cost_usd_subscription,
        unpriced_calls = 0,
        unpriced_calls_subscription = 0
    FROM jsonb_to_recordset(%s) AS u(
        event_id numeric,
        model text,
        cost_usd numeric,
        cost_usd_subscription numeric
    )
    WHERE o.event_id = u.event_id
      AND o.kind = 'usage'
      AND o.unpriced_calls > 0
      AND o.cost_usd = 0
    RETURNING o.agent_id, (o.occurred_at AT TIME ZONE 'UTC')::date AS day
"""

_RECOMPUTE_METRIC_DAYS_SQL = """
    UPDATE agent_metric_days d
    SET cost_usd = agg.cost_usd,
        cost_usd_subscription = agg.cost_usd_subscription,
        unpriced_calls = agg.unpriced_calls,
        unpriced_calls_subscription = agg.unpriced_calls_subscription
    FROM (
        SELECT o.agent_id,
               (o.occurred_at AT TIME ZONE 'UTC')::date AS day,
               COALESCE(sum(o.cost_usd), 0) AS cost_usd,
               COALESCE(sum(o.cost_usd_subscription), 0) AS cost_usd_subscription,
               COALESCE(sum(o.unpriced_calls), 0)::bigint AS unpriced_calls,
               COALESCE(sum(o.unpriced_calls_subscription), 0)::bigint AS unpriced_calls_subscription
        FROM agent_metric_observations o
        JOIN (
            SELECT DISTINCT agent_id, day
            FROM jsonb_to_recordset(%s) AS k(agent_id bigint, day date)
        ) affected
          ON o.agent_id = affected.agent_id
         AND o.occurred_at >= (affected.day::timestamp AT TIME ZONE 'UTC')
         AND o.occurred_at < ((affected.day + 1)::timestamp AT TIME ZONE 'UTC')
        GROUP BY o.agent_id, (o.occurred_at AT TIME ZONE 'UTC')::date
    ) agg
    WHERE d.agent_id = agg.agent_id AND d.day = agg.day
"""

_DAY_HAS_UNPRICED_SQL = f"""
    SELECT EXISTS (
        SELECT 1
        FROM telemetry_events t
        JOIN agents a ON a.id = t.agent_id
        WHERE t.ts >= %(start)s AND t.ts < %(end)s
          AND t.event_name = 'llm_usage'
          AND COALESCE({_COST}, '') = ''
    ) AS present
"""  # noqa: S608 — keys come from registered payload constants

_PRICED_DAY_SQL = f"""
    SELECT t.agent_id,
           COALESCE({LLM_USAGE_KEYS["model"]}, '') AS model,
           count(*)::bigint AS llm_calls,
           COALESCE(sum({numeric(LLM_USAGE_KEYS["in_total"])}), 0)::bigint AS tokens_in,
           COALESCE(sum({numeric(LLM_USAGE_KEYS["out_total"])}), 0)::bigint AS tokens_out,
           COALESCE(sum({numeric(LLM_USAGE_KEYS["cache_read"])}), 0)::bigint AS tokens_cached,
           COALESCE(sum({numeric(LLM_USAGE_KEYS["reasoning"])}), 0)::bigint AS tokens_reasoning,
           COALESCE(sum({numeric(_COST)}), 0)::float8 AS cost_usd,
           COALESCE(sum({numeric(_COST)}) FILTER (
               WHERE {LLM_USAGE_KEYS["source"]} = ANY(%(subscription)s)
           ), 0)::float8 AS cost_usd_subscription
    FROM telemetry_events t
    JOIN agents a ON a.id = t.agent_id
    WHERE t.ts >= %(start)s AND t.ts < %(end)s
      AND t.event_name = 'llm_usage'
      AND COALESCE({_COST}, '') <> ''
    GROUP BY t.agent_id, 2
"""  # noqa: S608 — keys come from registered payload constants

_UNPRICED_DAY_SQL = f"""
    SELECT t.agent_id,
           t.ts,
           COALESCE({LLM_USAGE_KEYS["model"]}, '') AS model,
           COALESCE({numeric(LLM_USAGE_KEYS["in_total"])}, 0)::bigint AS tokens_in,
           COALESCE({numeric(LLM_USAGE_KEYS["out_total"])}, 0)::bigint AS tokens_out,
           COALESCE({numeric(LLM_USAGE_KEYS["cache_read"])}, 0)::bigint AS tokens_cached,
           COALESCE({numeric(LLM_USAGE_KEYS["cache_write_5m"])}, 0)::bigint AS cache_write_5m,
           COALESCE({numeric(LLM_USAGE_KEYS["cache_write_1h"])}, 0)::bigint AS cache_write_1h,
           COALESCE({numeric(LLM_USAGE_KEYS["cache_write_30m"])}, 0)::bigint AS cache_write_30m,
           COALESCE({numeric(LLM_USAGE_KEYS["reasoning"])}, 0)::bigint AS tokens_reasoning,
           COALESCE({LLM_USAGE_KEYS["source"]} = ANY(%(subscription)s), false) AS is_subscription
    FROM telemetry_events t
    JOIN agents a ON a.id = t.agent_id
    WHERE t.ts >= %(start)s AND t.ts < %(end)s
      AND t.event_name = 'llm_usage'
      AND COALESCE({_COST}, '') = ''
"""  # noqa: S608 — keys come from registered payload constants

_EXISTING_LEDGER_DAY_SQL = """
    SELECT agent_id, model, llm_calls, estimated_calls
    FROM agent_model_tokens_daily
    WHERE day = %s
"""

_INSERT_LEDGER_ROWS_SQL = """
    INSERT INTO agent_model_tokens_daily (
        agent_id, day, model, llm_calls, tokens_in, tokens_out, tokens_cached,
        tokens_reasoning, cost_usd, cost_usd_subscription, costed_calls,
        unpriced_calls, unpriced_calls_subscription, estimated_calls
    )
    SELECT agent_id, %(day)s, model, llm_calls, tokens_in, tokens_out, tokens_cached,
           tokens_reasoning, cost_usd, cost_usd_subscription, costed_calls,
           unpriced_calls, unpriced_calls_subscription, estimated_calls
    FROM jsonb_to_recordset(%(rows)s) AS r(
        agent_id bigint,
        model text,
        llm_calls bigint,
        tokens_in bigint,
        tokens_out bigint,
        tokens_cached bigint,
        tokens_reasoning bigint,
        cost_usd double precision,
        cost_usd_subscription double precision,
        costed_calls bigint,
        unpriced_calls bigint,
        unpriced_calls_subscription bigint,
        estimated_calls bigint
    )
"""


def _chunks[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _quote_safe(
    model: str,
    tokens_in: int,
    tokens_out: int,
    cache: CacheTokens,
    *,
    at: datetime,
    prices: PriceBook,
) -> float | None:
    try:
        quoted = quote(model, tokens_in, tokens_out, cache, at=at, prices=prices)
    except ValueError:
        return None
    return None if quoted is None else quoted.cost_usd


def _fetch_cache_writes(
    conn: psycopg.Connection, rows: Sequence[_UnpricedObsRow]
) -> dict[tuple[int, datetime], tuple[int, int, int]]:
    writes: dict[tuple[int, datetime], tuple[int, int, int]] = {}
    for chunk in _chunks(rows, _TELEMETRY_LOOKUP_CHUNK_SIZE):
        uids = [event_uid(int(row.event_id)) for row in chunk]
        stamps = [row.occurred_at for row in chunk]
        for hit in query_all(
            conn, _CacheWriteRow, _CACHE_WRITE_LOOKUP_SQL.encode(), (uids, stamps)
        ):
            if hit.write_5m or hit.write_1h or hit.write_30m:
                writes[(hit.event_uid, hit.ts)] = (hit.write_5m, hit.write_1h, hit.write_30m)
    return writes


def _subscription_share(cost: Decimal, *, unpriced_calls: int, unpriced_sub: int) -> Decimal:
    if unpriced_sub <= 0:
        return Decimal(0)
    if unpriced_sub >= unpriced_calls:
        return cost
    return cost * Decimal(unpriced_sub) / Decimal(unpriced_calls)


def day_has_unpriced_usage(conn: psycopg.Connection, day: date) -> bool:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    row = query_one(
        conn,
        _ExistsRow,
        _DAY_HAS_UNPRICED_SQL.encode(),
        {"start": start, "end": start + timedelta(days=1)},
    )
    return bool(row is not None and row.present)


def _collect_ledger_buckets(
    priced_rows: Sequence[_PricedLedgerRow],
    unpriced_rows: Sequence[_UnpricedTelemetryRow],
    *,
    prices: PriceBook,
) -> tuple[dict[tuple[int, str], _LedgerBucket], int]:
    buckets: dict[tuple[int, str], _LedgerBucket] = {}
    for row in priced_rows:
        bucket = buckets.setdefault((row.agent_id, row.model), _LedgerBucket())
        bucket.llm_calls += row.llm_calls
        bucket.tokens_in += row.tokens_in
        bucket.tokens_out += row.tokens_out
        bucket.tokens_cached += row.tokens_cached
        bucket.tokens_reasoning += row.tokens_reasoning
        bucket.cost_usd += row.cost_usd
        bucket.cost_usd_subscription += row.cost_usd_subscription
        bucket.costed_calls += row.llm_calls

    calls_priced = 0
    for urow in unpriced_rows:
        canonical = resolve_harness_catalog_model(urow.model, prices) if urow.model else ""
        cost = (
            _quote_safe(
                canonical,
                urow.tokens_in,
                urow.tokens_out,
                CacheTokens(
                    read=urow.tokens_cached,
                    write_5m=urow.cache_write_5m,
                    write_1h=urow.cache_write_1h,
                    write_30m=urow.cache_write_30m,
                ),
                at=urow.ts,
                prices=prices,
            )
            if canonical
            else None
        )
        bucket = buckets.setdefault((urow.agent_id, canonical), _LedgerBucket())
        bucket.llm_calls += 1
        bucket.tokens_in += urow.tokens_in
        bucket.tokens_out += urow.tokens_out
        bucket.tokens_cached += urow.tokens_cached
        bucket.tokens_reasoning += urow.tokens_reasoning
        if cost is not None:
            calls_priced += 1
            bucket.cost_usd += cost
            if urow.is_subscription:
                bucket.cost_usd_subscription += cost
            bucket.costed_calls += 1
        else:
            bucket.unpriced_calls += 1
            if urow.is_subscription:
                bucket.unpriced_calls_subscription += 1
    return buckets, calls_priced


def _build_ledger_payload(
    buckets: dict[tuple[int, str], _LedgerBucket],
    existing: Sequence[_ExistingLedgerRow],
    *,
    prices: PriceBook,
) -> tuple[list[int], list[dict[str, object]]]:
    existing_calls_by_agent: dict[int, int] = defaultdict(int)
    existing_estimated: dict[tuple[int, str], int] = defaultdict(int)
    for ex in existing:
        existing_calls_by_agent[ex.agent_id] += ex.llm_calls
        canonical_ex = resolve_harness_catalog_model(ex.model, prices) if ex.model else ""
        existing_estimated[(ex.agent_id, canonical_ex)] += ex.estimated_calls

    by_agent: dict[int, list[tuple[str, _LedgerBucket]]] = defaultdict(list)
    for (agent_id, model), bucket in sorted(buckets.items()):
        by_agent[agent_id].append((model, bucket))

    passing_agents: list[int] = []
    payload: list[dict[str, object]] = []
    for agent_id, model_buckets in sorted(by_agent.items()):
        if sum(b.llm_calls for _, b in model_buckets) < existing_calls_by_agent.get(agent_id, 0):
            continue
        passing_agents.append(agent_id)
        for model, bucket in model_buckets:
            payload.append(
                {
                    "agent_id": agent_id,
                    "model": model,
                    "llm_calls": bucket.llm_calls,
                    "tokens_in": bucket.tokens_in,
                    "tokens_out": bucket.tokens_out,
                    "tokens_cached": bucket.tokens_cached,
                    "tokens_reasoning": bucket.tokens_reasoning,
                    "cost_usd": bucket.cost_usd,
                    "cost_usd_subscription": bucket.cost_usd_subscription,
                    "costed_calls": bucket.costed_calls,
                    "unpriced_calls": bucket.unpriced_calls,
                    "unpriced_calls_subscription": bucket.unpriced_calls_subscription,
                    "estimated_calls": existing_estimated.get((agent_id, model), 0),
                }
            )
    return passing_agents, payload


def roll_tokens_day_with_backfill(
    conn: psycopg.Connection,
    day: date,
    *,
    prices: PriceBook,
    dry_run: bool = False,
) -> tuple[int, int]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    params = {
        "start": start,
        "end": start + timedelta(days=1),
        "subscription": list(SUBSCRIPTION_HARNESSES),
    }
    priced_rows = query_all(conn, _PricedLedgerRow, _PRICED_DAY_SQL.encode(), params)
    unpriced_rows = query_all(conn, _UnpricedTelemetryRow, _UNPRICED_DAY_SQL.encode(), params)
    buckets, calls_priced = _collect_ledger_buckets(priced_rows, unpriced_rows, prices=prices)
    if not buckets:
        return 0, 0

    existing = query_all(conn, _ExistingLedgerRow, _EXISTING_LEDGER_DAY_SQL, (day,))
    passing_agents, payload = _build_ledger_payload(buckets, existing, prices=prices)
    if not passing_agents or dry_run:
        return len(payload), calls_priced

    with conn.transaction():
        _ = conn.execute(
            "DELETE FROM agent_model_tokens_daily WHERE day = %s AND agent_id = ANY(%s)",
            (day, passing_agents),
        )
        written = conn.execute(
            _INSERT_LEDGER_ROWS_SQL, {"day": day, "rows": Jsonb(payload)}
        ).rowcount
    return max(written, 0), calls_priced


def _totals_out_of_sync(conn: psycopg.Connection, *, through: date) -> bool:
    if through == date.min:
        return False
    total_row = query_one(
        conn,
        _UnpricedCountRow,
        "SELECT COALESCE(sum(unpriced_calls), 0)::bigint AS total FROM agent_model_tokens_total",
    )
    daily_row = query_one(
        conn,
        _UnpricedCountRow,
        "SELECT COALESCE(sum(unpriced_calls), 0)::bigint AS total "
        "FROM agent_model_tokens_daily WHERE day <= %s",
        (through,),
    )
    total_unpriced = 0 if total_row is None else total_row.total
    daily_unpriced = 0 if daily_row is None else daily_row.total
    return total_unpriced != daily_unpriced


@dataclass(frozen=True)
class _ObservationBackfillResult:
    scanned: int
    priced: int
    still_unpriced: int
    metric_days_recomputed: int
    added_cost_usd: Decimal
    added_cost_usd_subscription: Decimal
    by_model: tuple[UnpricedBackfillModelSummary, ...]


def _plan_observation_updates(
    obs_rows: Sequence[_UnpricedObsRow],
    cache_writes: dict[tuple[int, datetime], tuple[int, int, int]],
    *,
    book: PriceBook,
) -> tuple[
    list[dict[str, object]],
    set[tuple[int, date]],
    int,
    int,
    Decimal,
    Decimal,
    tuple[UnpricedBackfillModelSummary, ...],
]:
    updates: list[dict[str, object]] = []
    affected_days: set[tuple[int, date]] = set()
    obs_priced = 0
    obs_still_unpriced = 0
    added_cost = Decimal(0)
    added_cost_sub = Decimal(0)
    model_stats: dict[tuple[str, str], _ModelStat] = {}

    for row in obs_rows:
        raw_model = row.model or ""
        canonical = resolve_harness_catalog_model(raw_model, book) if raw_model else ""
        stat = model_stats.setdefault((raw_model, canonical), _ModelStat())
        w5m, w1h, w30m = cache_writes.get(
            (event_uid(int(row.event_id)), row.occurred_at), (0, 0, 0)
        )
        quoted = (
            _quote_safe(
                canonical,
                row.tokens_in,
                row.tokens_out,
                CacheTokens(read=row.tokens_cached, write_5m=w5m, write_1h=w1h, write_30m=w30m),
                at=row.occurred_at,
                prices=book,
            )
            if canonical
            else None
        )
        if quoted is None:
            obs_still_unpriced += 1
            stat.still_unpriced += 1
            continue
        cost = Decimal(str(quoted))
        cost_sub = _subscription_share(
            cost,
            unpriced_calls=row.unpriced_calls,
            unpriced_sub=row.unpriced_calls_subscription,
        )
        obs_priced += 1
        added_cost += cost
        added_cost_sub += cost_sub
        stat.priced += 1
        stat.added_cost_usd += cost
        updates.append(
            {
                "event_id": str(row.event_id),
                "model": canonical,
                "cost_usd": str(cost),
                "cost_usd_subscription": str(cost_sub),
            }
        )
        affected_days.add((row.agent_id, row.occurred_at.astimezone(UTC).date()))

    summaries = tuple(
        UnpricedBackfillModelSummary(
            raw_model=raw_model,
            canonical_model=canonical,
            observations_priced=stat.priced,
            observations_still_unpriced=stat.still_unpriced,
            added_cost_usd=stat.added_cost_usd,
        )
        for (raw_model, canonical), stat in sorted(model_stats.items())
    )
    return (
        updates,
        affected_days,
        obs_priced,
        obs_still_unpriced,
        added_cost,
        added_cost_sub,
        summaries,
    )


def _apply_observation_updates(
    conn: psycopg.Connection, updates: Sequence[dict[str, object]]
) -> set[tuple[int, date]]:
    recomputed_days: set[tuple[int, date]] = set()
    for chunk in _chunks(updates, _OBS_UPDATE_CHUNK_SIZE):
        with conn.transaction():
            returned = query_all(conn, _AffectedDayRow, _UPDATE_OBS_SQL, (Jsonb(list(chunk)),))
            chunk_days = sorted({(r.agent_id, r.day) for r in returned})
            if chunk_days:
                _ = conn.execute(
                    _RECOMPUTE_METRIC_DAYS_SQL,
                    (Jsonb([{"agent_id": aid, "day": d.isoformat()} for aid, d in chunk_days]),),
                )
                recomputed_days.update(chunk_days)
    return recomputed_days


def _backfill_observations(
    conn: psycopg.Connection, *, book: PriceBook, dry_run: bool
) -> _ObservationBackfillResult:
    obs_rows = query_all(conn, _UnpricedObsRow, _UNPRICED_OBS_SQL)
    cache_writes = _fetch_cache_writes(conn, obs_rows) if obs_rows else {}
    updates, affected_days, priced, still_unpriced, added_cost, added_cost_sub, summaries = (
        _plan_observation_updates(obs_rows, cache_writes, book=book)
    )
    recomputed_days = (
        affected_days if dry_run or not updates else _apply_observation_updates(conn, updates)
    )
    return _ObservationBackfillResult(
        scanned=len(obs_rows),
        priced=priced,
        still_unpriced=still_unpriced,
        metric_days_recomputed=len(recomputed_days),
        added_cost_usd=added_cost,
        added_cost_usd_subscription=added_cost_sub,
        by_model=summaries,
    )


def run_unpriced_backfill(
    conn: psycopg.Connection,
    *,
    today: date,
    prices: PriceBook | None = None,
    dry_run: bool = True,
) -> UnpricedBackfillReport:
    book = load_price_book() if prices is None else prices
    obs = _backfill_observations(conn, book=book, dry_run=dry_run)

    ledger_days = [
        r.day
        for r in query_all(
            conn,
            _DayRow,
            "SELECT DISTINCT day FROM agent_model_tokens_daily "
            "WHERE unpriced_calls > 0 AND day < %s ORDER BY day",
            (today,),
        )
    ]
    ledger_days_updated = 0
    ledger_calls_priced = 0
    updated_ledger_days: list[date] = []
    for day in ledger_days:
        written, priced_count = roll_tokens_day_with_backfill(
            conn, day, prices=book, dry_run=dry_run
        )
        if priced_count > 0:
            ledger_days_updated += 1
            ledger_calls_priced += priced_count
            if written > 0:
                updated_ledger_days.append(day)

    through = folded_through(conn)
    needs_rebuild = any(day <= through for day in updated_ledger_days) or _totals_out_of_sync(
        conn, through=through
    )
    totals_rebuilt = False
    if needs_rebuild and not dry_run:
        _ = rebuild_totals(conn, today=today)
        totals_rebuilt = True

    return UnpricedBackfillReport(
        dry_run=dry_run,
        observations_scanned=obs.scanned,
        observations_priced=obs.priced,
        observations_still_unpriced=obs.still_unpriced,
        metric_days_recomputed=obs.metric_days_recomputed,
        ledger_days_scanned=len(ledger_days),
        ledger_days_updated=ledger_days_updated,
        ledger_calls_priced=ledger_calls_priced,
        totals_rebuilt=totals_rebuilt,
        added_observation_cost_usd=obs.added_cost_usd,
        added_observation_cost_usd_subscription=obs.added_cost_usd_subscription,
        by_model=obs.by_model,
    )
