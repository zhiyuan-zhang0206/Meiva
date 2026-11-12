from __future__ import annotations

from datetime import datetime, timedelta

import psycopg

# meiva:note
# Retention of the stored transcript rows of external harness sessions (`agent_harness_transcript_rows`): a row
# is deleted when it is older than `TRANSCRIPT_ROW_RETENTION_DAYS` (a constant for now) **and** lies before its
# record's rebuild point (`agent_harness_transcript_positions.rebuild_ordinal`, the last quiet point: a restart
# rebuilds the projector from the stored rows after it). The rows after the rebuild point are never deleted
# however old, so a live session that has been idle for longer than the retention still restarts with all the
# data it needs; a record with no position row has nothing to rebuild and expires by age alone. Oldest first
# through the `written_at` index, in batches of at most `BATCH_ROWS` and at most `MAX_BATCHES` per pass, so a
# pass is a bounded statement and never a table scan; the next pass continues where this one stopped. Rows
# that are old but protected are skipped over by the index walk; they are the tail of an idle record, one
# turn's worth, so what a pass skips grows with the number of idle records and not with their length. The
# timeline (`agent_harness_messages`) is not touched: it is permanent, and the rows are what it is re-projected
# from only while they are kept.
# meiva:link future/agent/transcript-timeline-source.md
TRANSCRIPT_ROW_RETENTION_DAYS = 30
BATCH_ROWS = 5_000
MAX_BATCHES = 20

_DELETE_BATCH = """
DELETE FROM agent_harness_transcript_rows t USING (
    SELECT r.agent_id, r.session_id, r.record, r.ordinal
    FROM agent_harness_transcript_rows r
    LEFT JOIN agent_harness_transcript_positions p
        ON p.agent_id = r.agent_id AND p.session_id = r.session_id AND p.record = r.record
    WHERE r.written_at < %s AND (p.record IS NULL OR r.ordinal <= p.rebuild_ordinal)
    ORDER BY r.written_at LIMIT %s
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
