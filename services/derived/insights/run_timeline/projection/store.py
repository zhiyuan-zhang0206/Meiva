# meiva:link services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md
from __future__ import annotations

from datetime import datetime

from psycopg import Connection, sql

from services.derived.insights.run_timeline.projection.model import (
    VERSION,
    Fact,
    NodeRow,
    Projection,
    Segment,
)
from services.derived.insights.run_timeline.schemas import RunTimelineUnit

_SEGMENT_COLUMNS = """session, checkpoint_id, message_start, message_end, local_start,
    read_before, read_after, start_ts, end_ts, totals"""

_NODES = """
WITH tail AS MATERIALIZED (
    SELECT * FROM jsonb_to_recordset(%s::jsonb) AS f(
        idx integer, start_ts timestamptz, end_ts timestamptz,
        before_totals jsonb, after_totals jsonb
    )
)
SELECT to_jsonb(q) FROM (
    SELECT n.id, n.depth, n.parent_id, n.span_start, n.span_end, n.text, n.job_id, n.check_key,
           coalesce(a.start_ts, ta.start_ts) AS start,
           coalesce(b.end_ts, tb.end_ts) AS end,
           coalesce(a.before_totals, ta.before_totals) AS before,
           coalesce(b.after_totals, tb.after_totals) AS after
    FROM understanding_nodes n
    LEFT JOIN run_timeline_message_facts a
        ON a.agent_id = n.agent_id AND a.version = %s AND a.session < %s AND a.idx = n.span_start
    LEFT JOIN run_timeline_message_facts b
        ON b.agent_id = n.agent_id AND b.version = %s AND b.session < %s AND b.idx = n.span_end
    LEFT JOIN tail ta ON ta.idx = n.span_start
    LEFT JOIN tail tb ON tb.idx = n.span_end
    WHERE n.agent_id = %s AND n.span_start <= %s AND n.span_end >= %s AND n.span_end < %s
      AND n.start_ts IS NOT NULL AND n.end_ts IS NOT NULL
      AND coalesce(a.start_ts, ta.start_ts) <= %s AND coalesce(b.end_ts, tb.end_ts) >= %s
    ORDER BY n.depth, n.span_start
) q
"""


def last_segment(conn: Connection, agent_id: int) -> Segment | None:
    row = conn.execute(
        "SELECT to_jsonb(s) - 'agent_id' - 'version' FROM run_timeline_segments s"
        " WHERE agent_id = %s AND version = %s ORDER BY session DESC LIMIT 1",
        (agent_id, VERSION),
    ).fetchone()
    return Segment.model_validate(row[0]) if row else None


def publish(conn: Connection, agent_id: int, projection: Projection) -> None:
    segment = projection.segment
    data = segment.model_dump()
    data["totals"] = segment.totals.model_dump_json()
    conn.execute(
        sql.SQL(
            "INSERT INTO run_timeline_segments (agent_id, version, {})"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)"
        ).format(sql.SQL(_SEGMENT_COLUMNS)),
        (agent_id, VERSION, *data.values()),
    )
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO run_timeline_message_facts"
            " (agent_id, version, session, idx, start_ts, end_ts, request_ts, before_totals, after_totals)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)",
            [
                (
                    agent_id,
                    VERSION,
                    segment.session,
                    f.idx,
                    f.start_ts,
                    f.end_ts,
                    f.request_ts,
                    f.before_totals.model_dump_json(),
                    f.after_totals.model_dump_json(),
                )
                for f in projection.facts
            ],
        )
        cur.executemany(
            "INSERT INTO run_timeline_blocks"
            " (agent_id, version, session, ordinal, start_ts, end_ts, payload)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    agent_id,
                    VERSION,
                    segment.session,
                    i,
                    b.start,
                    b.end,
                    b.model_dump_json().encode("utf-8"),
                )
                for i, b in enumerate(projection.blocks)
            ],
        )


def node_index_bounds(
    conn: Connection,
    agent_id: int,
    sessions: int,
    start: datetime,
    end: datetime,
) -> tuple[int | None, int | None]:
    low = conn.execute(
        "SELECT idx FROM run_timeline_message_facts WHERE agent_id = %s AND version = %s"
        " AND session < %s AND end_ts >= %s ORDER BY end_ts, idx LIMIT 1",
        (agent_id, VERSION, sessions, start),
    ).fetchone()
    high = conn.execute(
        "SELECT idx FROM run_timeline_message_facts WHERE agent_id = %s AND version = %s"
        " AND session < %s AND start_ts <= %s ORDER BY start_ts DESC, idx DESC LIMIT 1",
        (agent_id, VERSION, sessions, end),
    ).fetchone()
    return (int(low[0]) if low else None, int(high[0]) if high else None)


def window_node_rows(
    conn: Connection,
    agent_id: int,
    cut: tuple[int, int, list[Fact]],
    bounds: tuple[int, int],
    start: datetime,
    end: datetime,
) -> list[NodeRow]:
    sessions, count, tail = cut
    tail_json = "[" + ",".join(f.model_dump_json() for f in tail) + "]"
    rows = conn.execute(
        _NODES,
        (
            tail_json,
            VERSION,
            sessions,
            VERSION,
            sessions,
            agent_id,
            bounds[1],
            bounds[0],
            count,
            end,
            start,
        ),
    ).fetchall()
    return [NodeRow.model_validate(row[0]) for row in rows]


def window_blocks(
    conn: Connection,
    agent_id: int,
    sessions: int,
    start: datetime,
    end: datetime,
    *,
    allowance: int | None,
) -> tuple[list[RunTimelineUnit], int]:
    where = (
        "WHERE agent_id = %s AND version = %s AND session < %s AND start_ts <= %s AND end_ts >= %s"
    )
    args = (agent_id, VERSION, sessions, end, start)
    row = conn.execute(
        sql.SQL("SELECT count(*) FROM run_timeline_blocks {}").format(sql.SQL(where)), args
    ).fetchone()
    count = int(row[0]) if row else 0
    if allowance is not None and count > allowance:
        return [], count
    rows = conn.execute(
        sql.SQL("SELECT payload FROM run_timeline_blocks {} ORDER BY session, ordinal").format(
            sql.SQL(where)
        ),
        args,
    ).fetchall()
    return [RunTimelineUnit.model_validate_json(bytes(row[0])) for row in rows], count


def segments_for_span(
    conn: Connection,
    agent_id: int,
    sessions: int,
    start: int,
    end: int,
) -> list[Segment]:
    rows = conn.execute(
        "SELECT to_jsonb(s) - 'agent_id' - 'version' FROM run_timeline_segments s"
        " WHERE agent_id = %s AND version = %s AND session < %s"
        " AND message_start <= %s AND message_end > %s ORDER BY session",
        (agent_id, VERSION, sessions, end, start),
    ).fetchall()
    return [Segment.model_validate(row[0]) for row in rows]
