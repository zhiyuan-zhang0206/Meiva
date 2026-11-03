# meiva:note
# Per-agent token sums for the fleet graph, read without scanning history.
#
# Three parts cover any window, and none grows with the age of the cluster:
#
# - the newest two UTC days (today and yesterday) from the raw `llm_usage` rows of `telemetry_events`,
#   which the ledger may not hold yet;
# - whole UTC days before those from the day-grain ledger `agent_model_tokens_daily` (a window that
#   starts mid-day also reads that first partial day from the raw rows);
# - for the all-time window, the folded whole-life sums `agent_model_tokens_total` up to the fold watermark,
#   then the ledger days after it (`services.upkeep.events_maintenance.token_totals`).
#
# A row is counted in exactly one part: the ledger days end before the raw tail starts, and a partial
# first day ends where the ledger days begin.

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple, SupportsFloat

import psycopg
from psycopg.rows import class_row

from base.events.contract import LLM_USAGE_KEYS
from base.telemetry.metrics.event_sql import numeric
from gateway.http.db_rows import query_all
from services.upkeep.events_maintenance.token_totals import folded_through

_RETAINED_WINDOW = timedelta(days=7)


# meiva:note
# One agent's llm_usage token sums: retained window (7d) and selected window.
class AgentTokens(NamedTuple):
    in_retained: float
    out_retained: float
    in_window: float
    out_window: float


# meiva:note
# How one window start splits into raw rows and ledger days.
@dataclass(frozen=True)
class _Plan:
    # meiva:note
    # raw rows at or after this instant
    raw_from: datetime
    # meiva:note
    # raw rows in [from, to): the first, partial day
    partial: tuple[datetime, datetime]
    # meiva:note
    # ledger days in [ledger_from, tail_day)
    ledger_from: date


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _plan(start: datetime, *, tail_day: date) -> _Plan:
    tail_start = _midnight(tail_day)
    if start >= tail_start:
        return _Plan(start, (start, start), tail_day)
    first_full = start.date() if start == _midnight(start.date()) else start.date() + timedelta(1)
    return _Plan(tail_start, (start, _midnight(first_full)), first_full)


# meiva:note
# One agent's four token sums (retained and window, input and output); the SQL sums are numeric,
# which psycopg hands back as `Decimal` or `int`, and only `float()` reads them.
@dataclass(frozen=True)
class _WindowSums:
    agent_id: int
    retained_in: SupportsFloat
    retained_out: SupportsFloat
    window_in: SupportsFloat
    window_out: SupportsFloat


# meiva:note
# One agent's all-time input and output token totals.
@dataclass(frozen=True)
class _TotalSums:
    agent_id: int
    tokens_in: SupportsFloat
    tokens_out: SupportsFloat


# meiva:note
# Per-agent token sums over the retained window and `win_start` (None = all time).
# meiva:note
# keys come from the registered payload constants.
def agent_tokens(
    conn: psycopg.Connection, *, now: datetime, win_start: datetime | None
) -> dict[int, AgentTokens]:
    tail_day = now.astimezone(UTC).date() - timedelta(days=1)
    retained = _plan(now - _RETAINED_WINDOW, tail_day=tail_day)
    if win_start is not None:
        window = _plan(win_start, tail_day=tail_day)
    else:
        tail_start = _midnight(tail_day)
        window = _Plan(tail_start, (tail_start, tail_start), folded_through(conn) + timedelta(1))
    sums: dict[int, list[float]] = {}

    def add(agent: int, values: tuple[SupportsFloat, ...]) -> None:
        row = sums.setdefault(agent, [0.0, 0.0, 0.0, 0.0])
        for slot, value in enumerate(values):
            row[slot] += float(value)

    in_total = numeric(LLM_USAGE_KEYS["in_total"])
    out_total = numeric(LLM_USAGE_KEYS["out_total"])

    def in_part(prefix: str) -> str:
        return f"(ts >= %({prefix}_raw)s OR (ts >= %({prefix}_pf)s AND ts < %({prefix}_pt)s))"

    raw_query = f"""
        SELECT agent_id,
               COALESCE(sum({in_total}) FILTER (WHERE {in_part("r")}), 0) AS retained_in,
               COALESCE(sum({out_total}) FILTER (WHERE {in_part("r")}), 0) AS retained_out,
               COALESCE(sum({in_total}) FILTER (WHERE {in_part("w")}), 0) AS window_in,
               COALESCE(sum({out_total}) FILTER (WHERE {in_part("w")}), 0) AS window_out
        FROM telemetry_events
        WHERE event_name = 'llm_usage' AND category = 'telemetry' AND agent_id IS NOT NULL
          AND ts <= %(now)s
          AND ({in_part("r")} OR {in_part("w")})
        GROUP BY agent_id
        """  # noqa: S608
    with conn.cursor(row_factory=class_row(_WindowSums)) as raw:
        raw.execute(
            raw_query,  # pyright: ignore[reportArgumentType]
            {
                "now": now,
                "r_raw": retained.raw_from,
                "r_pf": retained.partial[0],
                "r_pt": retained.partial[1],
                "w_raw": window.raw_from,
                "w_pf": window.partial[0],
                "w_pt": window.partial[1],
            },
        )
        for row in raw.fetchall():
            add(row.agent_id, (row.retained_in, row.retained_out, row.window_in, row.window_out))

    for row in query_all(
        conn,
        _WindowSums,
        """
        SELECT agent_id,
               COALESCE(sum(tokens_in) FILTER (WHERE day >= %(r_from)s), 0) AS retained_in,
               COALESCE(sum(tokens_out) FILTER (WHERE day >= %(r_from)s), 0) AS retained_out,
               COALESCE(sum(tokens_in) FILTER (WHERE day >= %(w_from)s), 0) AS window_in,
               COALESCE(sum(tokens_out) FILTER (WHERE day >= %(w_from)s), 0) AS window_out
        FROM agent_model_tokens_daily
        WHERE day >= LEAST(%(r_from)s, %(w_from)s) AND day < %(tail)s
        GROUP BY agent_id
        """,
        {"r_from": retained.ledger_from, "w_from": window.ledger_from, "tail": tail_day},
    ):
        add(row.agent_id, (row.retained_in, row.retained_out, row.window_in, row.window_out))

    if win_start is None:
        for total in query_all(
            conn,
            _TotalSums,
            "SELECT agent_id, sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out "
            "FROM agent_model_tokens_total GROUP BY agent_id",
        ):
            add(total.agent_id, (0, 0, total.tokens_in, total.tokens_out))
    return {agent: AgentTokens(*row) for agent, row in sums.items()}
