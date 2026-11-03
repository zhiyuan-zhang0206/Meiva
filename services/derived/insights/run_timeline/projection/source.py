# meiva:link services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from base.db.protocols import SyncConnection
from services.derived.insights.run_timeline.projection.model import VERSION, Segment
from services.derived.insights.run_timeline.projection.rows import rows_as


@dataclass(frozen=True)
class Source:
    head: str | None
    boundary: str | None
    boundaries: int
    previous: Segment | None

    @property
    def ready(self) -> bool:
        if self.boundaries == 0:
            return self.previous is None
        return self.previous is not None and self.previous.session + 1 == self.boundaries


class _SourceRow(NamedTuple):
    head: str | None
    boundary: str | None
    length: int
    segment: object


class _CountRow(NamedTuple):
    n: int


class _CheckpointRow(NamedTuple):
    checkpoint_id: str


def capture_source(conn: SyncConnection, agent_id: int) -> Source:
    with conn.cursor(row_factory=rows_as(_SourceRow)) as cur:
        cur.execute(
            """
            WITH head AS (
                SELECT max(checkpoint_id) AS id FROM checkpoints
                WHERE thread_id = %s AND checkpoint_ns = ''
            ), boundaries AS (
                SELECT count(*) AS count, max(checkpoint_id) AS id FROM checkpoints, head
                WHERE thread_id = %s AND checkpoint_ns = '' AND checkpoint_id <= head.id
                  AND metadata->>'compact_boundary' = 'true'
            )
            SELECT head.id AS head, boundaries.id AS boundary, boundaries.count AS length,
                   to_jsonb(s) - 'agent_id' - 'version' AS segment
            FROM head CROSS JOIN boundaries LEFT JOIN run_timeline_segments s
              ON s.agent_id = %s AND s.version = %s AND s.checkpoint_id = boundaries.id
            """,
            (str(agent_id), str(agent_id), agent_id, VERSION),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("timeline source query returned no aggregate row")
    previous = Segment.model_validate(row.segment) if row.segment else None
    return Source(row.head, row.boundary, row.length, previous)


def next_boundary(conn: SyncConnection, agent_id: int, previous: Segment | None) -> str | None:
    through = previous.checkpoint_id if previous else ""
    if previous is not None:
        with conn.cursor(row_factory=rows_as(_CountRow)) as cur:
            cur.execute(
                "SELECT count(*) AS n FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = ''"
                " AND checkpoint_id <= %s AND metadata->>'compact_boundary' = 'true'",
                (str(agent_id), through),
            )
            counted = cur.fetchone()
        if counted is None or counted.n != previous.session + 1:
            raise RuntimeError(
                "retained compaction prefix changed; rebuild the timeline projection"
            )
    with conn.cursor(row_factory=rows_as(_CheckpointRow)) as cur:
        cur.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = ''"
            " AND checkpoint_id > %s AND metadata->>'compact_boundary' = 'true'"
            " ORDER BY checkpoint_id LIMIT 1",
            (str(agent_id), through),
        )
        row = cur.fetchone()
    return row.checkpoint_id if row else None
