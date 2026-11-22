from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from base.agents.model.harness_models import (
    resolve_harness_catalog_model as resolve_harness_catalog_model,
)
from base.api_contracts.agent_contract import SUBSCRIPTION_HARNESSES
from base.db.rows import query_all, query_one
from base.events.contract import LLM_USAGE_KEYS
from base.lm.pricing import PriceBook
from base.lm.pricing.archive import load_archive
from base.telemetry.event_store import event_uid
from base.telemetry.metrics.event_sql import numeric
from services.upkeep.events_maintenance.token_totals import folded_through, rebuild_totals
from services.upkeep.events_maintenance.unpriced_backfill_plan import (
    ExistingLedgerRow,
    LedgerBucket,
    PricedLedgerRow,
    UnpricedObsRow,
    UnpricedTelemetryRow,
    build_ledger_payload,
    collect_ledger_buckets,
    plan_observation_updates,
)
from services.upkeep.events_maintenance.unpriced_backfill_plan import (
    UnpricedBackfillModelSummary as UnpricedBackfillModelSummary,
)
from services.upkeep.events_maintenance.unpriced_backfill_plan import (
    UnpricedBackfillReport as UnpricedBackfillReport,
)

_OBS_UPDATE_CHUNK_SIZE = 1000
_TELEMETRY_LOOKUP_CHUNK_SIZE = 1000
_LEDGER_DAY_LOCK_NS = 4_730_002

_COST = LLM_USAGE_KEYS["cost_usd"]


def load_price_book() -> PriceBook:
    return PriceBook(load_archive(), {})


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
class _DayAgentRow:
    day: date
    agent_id: int


@dataclass(frozen=True)
class _ExistsRow:
    present: bool


@dataclass(frozen=True)
class _UnpricedCountRow:
    total: int


@dataclass(frozen=True)
class _ObservationBackfillResult:
    scanned: int
    priced: int
    still_unpriced: int
    metric_days_recomputed: int
    added_cost_usd: Decimal
    added_cost_usd_subscription: Decimal
    by_model: tuple[UnpricedBackfillModelSummary, ...]


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

_APPLY_OBS_BACKFILL_SQL = """
    WITH updated AS (
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
            cost_usd_subscription numeric,
            unpriced_calls bigint,
            unpriced_calls_subscription bigint
        )
        WHERE o.event_id = u.event_id
          AND o.kind = 'usage'
          AND o.cost_usd = 0
          AND o.unpriced_calls = u.unpriced_calls
          AND o.unpriced_calls_subscription = u.unpriced_calls_subscription
        RETURNING o.agent_id,
                  (o.occurred_at AT TIME ZONE 'UTC')::date AS day,
                  u.cost_usd AS delta_cost_usd,
                  u.cost_usd_subscription AS delta_cost_usd_subscription,
                  u.unpriced_calls AS delta_unpriced_calls,
                  u.unpriced_calls_subscription AS delta_unpriced_calls_subscription
    ), deltas AS (
        SELECT agent_id,
               day,
               sum(delta_cost_usd) AS delta_cost_usd,
               sum(delta_cost_usd_subscription) AS delta_cost_usd_subscription,
               sum(delta_unpriced_calls)::bigint AS delta_unpriced_calls,
               sum(delta_unpriced_calls_subscription)::bigint AS delta_unpriced_calls_subscription
        FROM updated
        GROUP BY agent_id, day
        ORDER BY agent_id, day
    ), days_updated AS (
        UPDATE agent_metric_days d
        SET cost_usd = d.cost_usd + deltas.delta_cost_usd,
            cost_usd_subscription = d.cost_usd_subscription + deltas.delta_cost_usd_subscription,
            unpriced_calls = GREATEST(0, d.unpriced_calls - deltas.delta_unpriced_calls),
            unpriced_calls_subscription = GREATEST(
                0, d.unpriced_calls_subscription - deltas.delta_unpriced_calls_subscription
            )
        FROM deltas
        WHERE d.agent_id = deltas.agent_id AND d.day = deltas.day
        RETURNING d.agent_id, d.day
    )
    SELECT agent_id, day FROM days_updated
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
      AND (%(agent_ids)s::bigint[] IS NULL OR t.agent_id = ANY(%(agent_ids)s::bigint[]))
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
      AND (%(agent_ids)s::bigint[] IS NULL OR t.agent_id = ANY(%(agent_ids)s::bigint[]))
"""  # noqa: S608 — keys come from registered payload constants

_EXISTING_LEDGER_DAY_SQL = """
    SELECT agent_id, model, llm_calls, tokens_in, tokens_out, tokens_cached,
           tokens_reasoning, cost_usd, cost_usd_subscription, costed_calls,
           unpriced_calls, unpriced_calls_subscription, estimated_calls
    FROM agent_model_tokens_daily
    WHERE day = %(day)s
      AND (%(agent_ids)s::bigint[] IS NULL OR agent_id = ANY(%(agent_ids)s::bigint[]))
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


def _fetch_cache_writes(
    conn: psycopg.Connection, rows: Sequence[UnpricedObsRow]
) -> dict[Decimal, tuple[int, int, int]]:
    writes: dict[Decimal, tuple[int, int, int]] = {}
    for chunk in _chunks(rows, _TELEMETRY_LOOKUP_CHUNK_SIZE):
        id_by_key: dict[tuple[int, datetime], list[Decimal]] = defaultdict(list)
        uids: list[int] = []
        stamps: list[datetime] = []
        for row in chunk:
            uid = event_uid(int(row.event_id))
            uids.append(uid)
            stamps.append(row.occurred_at)
            id_by_key[(uid, row.occurred_at)].append(row.event_id)
        for hit in query_all(
            conn, _CacheWriteRow, _CACHE_WRITE_LOOKUP_SQL.encode(), (uids, stamps)
        ):
            if hit.write_5m or hit.write_1h or hit.write_30m:
                for eid in id_by_key.get((hit.event_uid, hit.ts), ()):
                    writes[eid] = (hit.write_5m, hit.write_1h, hit.write_30m)
    return writes


def day_has_unpriced_usage(conn: psycopg.Connection, day: date) -> bool:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    row = query_one(
        conn,
        _ExistsRow,
        _DAY_HAS_UNPRICED_SQL.encode(),
        {"start": start, "end": start + timedelta(days=1)},
    )
    return bool(row is not None and row.present)


def _read_day_buckets(
    conn: psycopg.Connection,
    day: date,
    *,
    prices: PriceBook,
    agent_ids: list[int] | None,
) -> dict[tuple[int, str], LedgerBucket]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    params: dict[str, object] = {
        "start": start,
        "end": start + timedelta(days=1),
        "subscription": list(SUBSCRIPTION_HARNESSES),
        "agent_ids": agent_ids,
    }
    priced_rows = query_all(conn, PricedLedgerRow, _PRICED_DAY_SQL.encode(), params)
    unpriced_rows = query_all(conn, UnpricedTelemetryRow, _UNPRICED_DAY_SQL.encode(), params)
    return collect_ledger_buckets(priced_rows, unpriced_rows, prices=prices)


def roll_tokens_day_with_backfill(
    conn: psycopg.Connection,
    day: date,
    *,
    prices: PriceBook,
    dry_run: bool = False,
    agent_ids: Sequence[int] | None = None,
    only_unpriced_models: bool = False,
) -> tuple[int, int]:
    scoped_agents = None if agent_ids is None else sorted(set(agent_ids))
    buckets = _read_day_buckets(conn, day, prices=prices, agent_ids=scoped_agents)
    if not buckets:
        return 0, 0

    existing_params: dict[str, object] = {"day": day, "agent_ids": scoped_agents}
    if dry_run:
        existing = query_all(conn, ExistingLedgerRow, _EXISTING_LEDGER_DAY_SQL, existing_params)
        changed_agents, payload, calls_priced = build_ledger_payload(
            buckets, existing, prices=prices, only_unpriced_models=only_unpriced_models
        )
        return (len(payload) if changed_agents else 0), calls_priced

    with conn.transaction():
        _ = conn.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (_LEDGER_DAY_LOCK_NS, day.toordinal()),
        )
        existing = query_all(conn, ExistingLedgerRow, _EXISTING_LEDGER_DAY_SQL, existing_params)
        changed_agents, payload, calls_priced = build_ledger_payload(
            buckets, existing, prices=prices, only_unpriced_models=only_unpriced_models
        )
        if not changed_agents:
            return 0, 0
        _ = conn.execute(
            "DELETE FROM agent_model_tokens_daily WHERE day = %s AND agent_id = ANY(%s)",
            (day, changed_agents),
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


def _apply_observation_updates(
    conn: psycopg.Connection, updates: Sequence[dict[str, object]]
) -> set[tuple[int, date]]:
    recomputed_days: set[tuple[int, date]] = set()
    for chunk in _chunks(updates, _OBS_UPDATE_CHUNK_SIZE):
        with conn.transaction():
            returned = query_all(
                conn, _AffectedDayRow, _APPLY_OBS_BACKFILL_SQL, (Jsonb(list(chunk)),)
            )
            recomputed_days.update((r.agent_id, r.day) for r in returned)
    return recomputed_days


def _backfill_observations(
    conn: psycopg.Connection, *, book: PriceBook, dry_run: bool
) -> _ObservationBackfillResult:
    obs_rows = query_all(conn, UnpricedObsRow, _UNPRICED_OBS_SQL)
    cache_writes = _fetch_cache_writes(conn, obs_rows) if obs_rows else {}
    plan = plan_observation_updates(obs_rows, cache_writes, book=book)
    recomputed_days = (
        plan.affected_days
        if dry_run or not plan.updates
        else _apply_observation_updates(conn, plan.updates)
    )
    return _ObservationBackfillResult(
        scanned=len(obs_rows),
        priced=plan.priced,
        still_unpriced=plan.still_unpriced,
        metric_days_recomputed=len(recomputed_days),
        added_cost_usd=plan.added_cost_usd,
        added_cost_usd_subscription=plan.added_cost_usd_subscription,
        by_model=plan.by_model,
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

    agents_by_day: dict[date, list[int]] = defaultdict(list)
    for pair in query_all(
        conn,
        _DayAgentRow,
        "SELECT DISTINCT day, agent_id FROM agent_model_tokens_daily "
        "WHERE unpriced_calls > 0 AND day < %s ORDER BY day, agent_id",
        (today,),
    ):
        agents_by_day[pair.day].append(pair.agent_id)

    ledger_days_updated = 0
    ledger_calls_priced = 0
    updated_ledger_days: list[date] = []
    for day, agent_ids in agents_by_day.items():
        written, priced_count = roll_tokens_day_with_backfill(
            conn,
            day,
            prices=book,
            dry_run=dry_run,
            agent_ids=agent_ids,
            only_unpriced_models=True,
        )
        if written > 0:
            ledger_days_updated += 1
            ledger_calls_priced += priced_count
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
        ledger_days_scanned=len(agents_by_day),
        ledger_days_updated=ledger_days_updated,
        ledger_calls_priced=ledger_calls_priced,
        totals_rebuilt=totals_rebuilt,
        added_observation_cost_usd=obs.added_cost_usd,
        added_observation_cost_usd_subscription=obs.added_cost_usd_subscription,
        by_model=obs.by_model,
    )
