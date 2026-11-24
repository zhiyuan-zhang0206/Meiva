# meiva:link base/lm/usage/README.md
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Literal

import psycopg
from psycopg.rows import class_row

from base.api_contracts.usage import UsageLedgerEntry
from base.db.rows import query_all, query_one
from base.db.usage_ledger import insert_usage

CALL_ID_PREFIX = "legacy-daily:"
USAGE_KIND = "legacy_daily"
MACHINE = "legacy-daily"

Billing = Literal["api", "subscription", "mixed"]

_CUTOFF = """
    SELECT min((occurred_at AT TIME ZONE 'UTC')::date) AS day FROM llm_usage_ledger
    WHERE call_id NOT LIKE 'legacy-daily:%%'
"""
_PAGE = """
    SELECT agent_id, day, model, llm_calls, tokens_in, tokens_out, tokens_cached, tokens_reasoning,
           cost_usd, cost_usd_subscription, costed_calls, unpriced_calls,
           unpriced_calls_subscription, estimated_calls
    FROM agent_model_tokens_daily
    WHERE day < %(cutoff)s AND (day, agent_id, model) > (%(day)s, %(agent)s, %(model)s)
    ORDER BY day, agent_id, model
    LIMIT %(limit)s
"""
_OVERLAP = """
    SELECT d.day, d.calls AS daily_calls, COALESCE(l.calls, 0) AS ledger_calls
    FROM (SELECT day, sum(llm_calls)::bigint AS calls FROM agent_model_tokens_daily
          WHERE day >= %(cutoff)s GROUP BY day) d
    LEFT JOIN (SELECT (occurred_at AT TIME ZONE 'UTC')::date AS day, sum(calls)::bigint AS calls
               FROM llm_usage_ledger WHERE call_id NOT LIKE 'legacy-daily:%%' GROUP BY 1) l
      ON l.day = d.day
    ORDER BY d.day
"""
_TOTAL_CHECK = """
    SELECT count(*) AS keys, count(*) FILTER (WHERE t.llm_calls IS DISTINCT FROM d.llm_calls
                                              OR t.tokens_in IS DISTINCT FROM d.tokens_in) AS mismatched
    FROM (SELECT agent_id, model, sum(llm_calls)::bigint AS llm_calls, sum(tokens_in)::bigint AS tokens_in
          FROM agent_model_tokens_daily WHERE day <= (SELECT day FROM agent_model_tokens_total_through)
          GROUP BY agent_id, model) d
    FULL JOIN agent_model_tokens_total t USING (agent_id, model)
"""
_IMPORTED = """
    SELECT billing, count(*) AS reports, COALESCE(sum(calls), 0)::bigint AS calls,
           COALESCE(sum(tokens_in), 0)::bigint AS tokens_in,
           COALESCE(sum(tokens_out), 0)::bigint AS tokens_out,
           COALESCE(sum(cost_usd), 0)::text AS cost_usd
    FROM llm_usage_ledger WHERE call_id LIKE 'legacy-daily:%%' GROUP BY billing
"""


@dataclass(frozen=True, slots=True)
class DayRow:
    agent_id: int
    day: date
    model: str
    llm_calls: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    tokens_reasoning: int
    cost_usd: float
    cost_usd_subscription: float
    costed_calls: int
    unpriced_calls: int
    unpriced_calls_subscription: int
    estimated_calls: int


def billing_of(row: DayRow) -> Billing:
    if row.cost_usd_subscription == 0 and row.unpriced_calls_subscription == 0:
        return "api"
    if (
        math.isclose(row.cost_usd_subscription, row.cost_usd, rel_tol=1e-9, abs_tol=1e-12)
        and row.unpriced_calls_subscription == row.unpriced_calls
    ):
        return "subscription"
    return "mixed"


def entry_of(
    row: DayRow, *, billing: Literal["api", "subscription"], cluster: str
) -> UsageLedgerEntry:
    priced = row.unpriced_calls < row.llm_calls
    return UsageLedgerEntry(
        call_id=f"{CALL_ID_PREFIX}{row.day.isoformat()}:{row.agent_id}:{row.model}",
        occurred_at=datetime.combine(row.day, time.min, tzinfo=UTC),
        agent_id=row.agent_id,
        machine=MACHINE,
        cluster=cluster,
        source=None,
        billing=billing,
        usage_kind=USAGE_KIND,
        model=row.model,
        requested_model=None,
        provider=None,
        provider_request_id=None,
        calls=row.llm_calls,
        complete=not 0 < row.unpriced_calls < row.llm_calls,
        tokens_in=row.tokens_in,
        tokens_out=row.tokens_out,
        tokens_reasoning=row.tokens_reasoning,
        cache_read=row.tokens_cached,
        cache_write_5m=None,
        cache_write_1h=None,
        cache_write_30m=None,
        cost_usd=Decimal(str(row.cost_usd)) if priced else None,
        price_snapshot={},
        price_source="legacy" if priced else None,
    )


@dataclass(slots=True)
class Totals:
    rows: int = 0
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)


@dataclass(slots=True)
class Scan:
    cutoff: date
    first_day: date | None = None
    last_day: date | None = None
    rows: int = 0
    skipped_zero_call_rows: int = 0
    unpriced_rows: int = 0
    unpriced_calls: int = 0
    partly_priced_rows: int = 0
    partly_priced_unpriced_calls: int = 0
    estimated_calls: int = 0
    by_billing: dict[str, Totals] = field(default_factory=lambda: defaultdict(Totals))
    by_model: dict[tuple[str, str], Totals] = field(default_factory=lambda: defaultdict(Totals))
    mixed: list[DayRow] = field(default_factory=list)


def _page(
    conn: psycopg.Connection, cutoff: date, after: tuple[date, int, str], limit: int
) -> list[DayRow]:
    with conn.cursor(row_factory=class_row(DayRow)) as cur:
        cur.execute(
            _PAGE,
            {
                "cutoff": cutoff,
                "day": after[0],
                "agent": after[1],
                "model": after[2],
                "limit": limit,
            },
        )
        return cur.fetchall()


@dataclass(frozen=True, slots=True)
class _OptionalDay:
    day: date | None


@dataclass(frozen=True, slots=True)
class _OverlapRow:
    day: date
    daily_calls: int
    ledger_calls: int


@dataclass(frozen=True, slots=True)
class _TotalCheckRow:
    keys: int
    mismatched: int


@dataclass(frozen=True, slots=True)
class _ImportedRow:
    billing: str
    reports: int
    calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: str


def ledger_cutoff(conn: psycopg.Connection) -> date:
    row = query_one(conn, _OptionalDay, _CUTOFF)
    if row is None or row.day is None:
        raise ValueError("the usage ledger holds no per-call report: nothing marks where it starts")
    return row.day


def _walk(
    conn: psycopg.Connection, cutoff: date, batch_size: int, visit: Callable[[list[DayRow]], None]
) -> None:
    after: tuple[date, int, str] = (date.min, 0, "")
    while rows := _page(conn, cutoff, after, batch_size):
        visit(rows)
        last = rows[-1]
        after = (last.day, last.agent_id, last.model)


def scan(conn: psycopg.Connection, *, batch_size: int) -> Scan:
    result = Scan(cutoff=ledger_cutoff(conn))

    def visit(rows: list[DayRow]) -> None:
        for row in rows:
            if row.llm_calls <= 0:
                result.skipped_zero_call_rows += 1
                continue
            billing = billing_of(row)
            result.rows += 1
            result.first_day = min(result.first_day or row.day, row.day)
            result.last_day = max(result.last_day or row.day, row.day)
            result.estimated_calls += row.estimated_calls
            if row.unpriced_calls >= row.llm_calls:
                result.unpriced_rows += 1
                result.unpriced_calls += row.llm_calls
            elif row.unpriced_calls > 0:
                result.partly_priced_rows += 1
                result.partly_priced_unpriced_calls += row.unpriced_calls
            if billing == "mixed":
                result.mixed.append(row)
            for totals in (result.by_billing[billing], result.by_model[(billing, row.model)]):
                totals.rows += 1
                totals.calls += row.llm_calls
                totals.tokens_in += row.tokens_in
                totals.tokens_out += row.tokens_out
                totals.cost_usd += Decimal(str(row.cost_usd))

    _walk(conn, result.cutoff, batch_size, visit)
    return result


def overlap_days(conn: psycopg.Connection, cutoff: date) -> list[dict[str, object]]:
    rows = query_all(conn, _OverlapRow, _OVERLAP, {"cutoff": cutoff})
    return [
        {"day": r.day.isoformat(), "daily_calls": r.daily_calls, "ledger_calls": r.ledger_calls}
        for r in rows
    ]


def total_check(conn: psycopg.Connection) -> dict[str, object] | None:
    through = query_one(conn, _OptionalDay, "SELECT day FROM agent_model_tokens_total_through")
    if through is None or through.day is None:
        return None
    check = query_one(conn, _TotalCheckRow, _TOTAL_CHECK)
    return {
        "through": through.day.isoformat(),
        "keys": check.keys if check else 0,
        "mismatched_keys": check.mismatched if check else 0,
    }


def imported_totals(conn: psycopg.Connection) -> dict[str, dict[str, object]]:
    return {
        r.billing: {
            "reports": r.reports,
            "calls": r.calls,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "cost_usd": r.cost_usd,
        }
        for r in query_all(conn, _ImportedRow, _IMPORTED)
    }


class MixedBillingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ImportCounts:
    inserted: int
    existing: int


def import_days(
    conn: psycopg.Connection, *, cutoff: date, cluster: str, batch_size: int
) -> ImportCounts:
    mixed = sum(1 for _ in _mixed_rows(conn, cutoff, batch_size))
    if mixed:
        raise MixedBillingError(f"{mixed} daily rows mix API and subscription calls")
    counts = [0, 0]

    def visit(rows: list[DayRow]) -> None:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ WRITE")
            conn.execute("SET LOCAL synchronous_commit TO on")
            for row in rows:
                if row.llm_calls <= 0:
                    continue
                billing = billing_of(row)
                if billing == "mixed":
                    raise MixedBillingError(f"{row.day} {row.agent_id} {row.model} mixes billings")
                inserted = insert_usage(
                    conn, entry_of(row, billing=billing, cluster=cluster), check_replay=False
                )
                counts[0 if inserted else 1] += 1

    _walk(conn, cutoff, batch_size, visit)
    return ImportCounts(counts[0], counts[1])


def _mixed_rows(conn: psycopg.Connection, cutoff: date, batch_size: int) -> list[DayRow]:
    found: list[DayRow] = []
    _walk(
        conn,
        cutoff,
        batch_size,
        lambda rows: found.extend(r for r in rows if r.llm_calls > 0 and billing_of(r) == "mixed"),
    )
    return found
