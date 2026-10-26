# meiva:link services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md
from __future__ import annotations

from dataclasses import dataclass

from base.db.protocols import SyncConnection
from services.derived.insights.run_timeline.projection.model import VERSION, Segment


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


def capture_source(conn: SyncConnection, agent_id: int) -> Source:
    row = conn.execute(
        """
        WITH head AS (
            SELECT max(checkpoint_id) AS id FROM checkpoints
            WHERE thread_id = %s AND checkpoint_ns = ''
        ), boundaries AS (
            SELECT count(*) AS count, max(checkpoint_id) AS id FROM checkpoints, head
            WHERE thread_id = %s AND checkpoint_ns = '' AND checkpoint_id <= head.id
              AND metadata->>'compact_boundary' = 'true'
        )
        SELECT head.id, boundaries.id, boundaries.count,
               to_jsonb(s) - 'agent_id' - 'version'
        FROM head CROSS JOIN boundaries LEFT JOIN run_timeline_segments s
          ON s.agent_id = %s AND s.version = %s AND s.checkpoint_id = boundaries.id
        """,
        (str(agent_id), str(agent_id), agent_id, VERSION),
    ).fetchone()
    if row is None:
        raise RuntimeError("timeline source query returned no aggregate row")
    head, boundary, count, segment = row
    previous = Segment.model_validate(segment) if segment else None
    return Source(head, boundary, count, previous)


def next_boundary(conn: SyncConnection, agent_id: int, previous: Segment | None) -> str | None:
    through = previous.checkpoint_id if previous else ""
    if previous is not None:
        row = conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = ''"
            " AND checkpoint_id <= %s AND metadata->>'compact_boundary' = 'true'",
            (str(agent_id), through),
        ).fetchone()
        if row is None or row[0] != previous.session + 1:
            raise RuntimeError(
                "retained compaction prefix changed; rebuild the timeline projection"
            )
    row = conn.execute(
        "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = ''"
        " AND checkpoint_id > %s AND metadata->>'compact_boundary' = 'true'"
        " ORDER BY checkpoint_id LIMIT 1",
        (str(agent_id), through),
    ).fetchone()
    return str(row[0]) if row else None
