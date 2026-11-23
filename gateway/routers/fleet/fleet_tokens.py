from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple

import psycopg

from base.db.rows import query_all

_RETAINED_WINDOW = timedelta(days=7)


class AgentTokens(NamedTuple):
    in_retained: float
    out_retained: float
    in_window: float
    out_window: float


@dataclass(frozen=True)
class _WindowSums:
    agent_id: int
    retained_in: float
    retained_out: float
    window_in: float
    window_out: float


# meiva:link gateway/cluster/docs/ops-surfaces.md
def agent_tokens(
    conn: psycopg.Connection, *, now: datetime, win_start: datetime | None
) -> dict[int, AgentTokens]:
    retained = now - _RETAINED_WINDOW
    rows = query_all(
        conn,
        _WindowSums,
        """
        SELECT agent_id,
          COALESCE(sum(tokens_in) FILTER (WHERE occurred_at >= %(retained)s), 0) AS retained_in,
          COALESCE(sum(tokens_out) FILTER (WHERE occurred_at >= %(retained)s), 0) AS retained_out,
          COALESCE(sum(tokens_in) FILTER (WHERE occurred_at >= %(start)s), 0) AS window_in,
          COALESCE(sum(tokens_out) FILTER (WHERE occurred_at >= %(start)s), 0) AS window_out
        FROM llm_usage_ledger
        WHERE agent_id IS NOT NULL AND occurred_at < %(now)s AND occurred_at >= %(earliest)s
        GROUP BY agent_id
        """,
        {
            "retained": retained,
            "start": win_start if win_start is not None else "-infinity",
            "now": now,
            "earliest": min(retained, win_start) if win_start is not None else "-infinity",
        },
    )
    return {
        row.agent_id: AgentTokens(
            float(row.retained_in),
            float(row.retained_out),
            float(row.window_in),
            float(row.window_out),
        )
        for row in rows
    }
