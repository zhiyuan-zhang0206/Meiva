from __future__ import annotations

from datetime import datetime, timedelta

import psycopg

# meiva:note
# Retention of the stored transcript rows of external harness sessions (`agent_harness_transcript_rows`): rows
# older than `TRANSCRIPT_ROW_RETENTION_DAYS` are deleted, oldest first through the `written_at` index, in
# batches of at most `BATCH_ROWS` and at most `MAX_BATCHES` per pass, so a pass is a bounded statement and
# never a table scan; the next pass continues where this one stopped. The timeline (`agent_harness_messages`) is
# not touched: it is permanent, and the rows are what it is re-projected from only while they are kept.
# meiva:link future/agent/transcript-timeline-source.md
TRANSCRIPT_ROW_RETENTION_DAYS = 30
BATCH_ROWS = 5_000
MAX_BATCHES = 20

_DELETE_BATCH = """
DELETE FROM agent_harness_transcript_rows t USING (
    SELECT agent_id, session_id, record, ordinal FROM agent_harness_transcript_rows
    WHERE written_at < %s ORDER BY written_at LIMIT %s
) old
WHERE t.agent_id = old.agent_id AND t.session_id = old.session_id
  AND t.record = old.record AND t.ordinal = old.ordinal
"""


def sweep_expired_transcript_rows(
    conn: psycopg.Connection,
    *,
    now: datetime,
    retention_days: int = TRANSCRIPT_ROW_RETENTION_DAYS,
    batch_rows: int = BATCH_ROWS,
    max_batches: int = MAX_BATCHES,
) -> int:
    cutoff = now - timedelta(days=retention_days)
    deleted = 0
    for _ in range(max_batches):
        with conn.cursor() as cur:
            _ = cur.execute(_DELETE_BATCH, (cutoff, batch_rows))
            removed = cur.rowcount
        conn.commit()
        deleted += removed
        if removed < batch_rows:
            break
    return deleted
