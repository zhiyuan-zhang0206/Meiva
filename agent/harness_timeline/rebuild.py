from __future__ import annotations

from collections.abc import Mapping

from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.store import aload_transcript_row_floor, aload_transcript_rows
from base.host.boundary.json_values import is_object_dict
from base.lm.harness.session.transcript.ports import RecordReader, StoredRow
from base.lm.harness.session.transcript.projection import Projector
from base.log import logger

REBUILD_BATCH_ROWS = 500
REBUILD_MAX_ROWS = 20_000


def _mapping(row: object) -> Mapping[str, object]:
    if not is_object_dict(row):
        raise TypeError("a stored transcript row is not an object")
    return row


# meiva:note
# Feed one record's stored rows after `after_ordinal` (the record's rebuild point) to `projector`, in bounded batches
# of `REBUILD_BATCH_ROWS` and without touching the CLI's file, and return the ordinal of the last row fed (None when
# there was none). A turn that has gone more than `max_rows` stored rows without a quiet point is rebuilt from its
# latest `max_rows` only and says so (`harness_timeline_rebuild_bounded`): a call that began earlier is then not known,
# so its output is held for a bounded time and shown top-level. The projector is the caller's own: a rebuild that fails
# leaves the writer's projector as it was.
# meiva:link agent/harness_timeline/README.md
async def rebuild_record(
    pool: AsyncConnectionPool,
    reader: RecordReader,
    projector: Projector,
    *,
    agent_id: int,
    sid: str,
    record: str,
    after_ordinal: int,
    max_rows: int,
) -> int | None:
    cursor = await _floor(pool, agent_id, sid, record, after_ordinal, max_rows)
    last: int | None = None
    while True:
        async with pool.connection() as conn:
            loaded = await aload_transcript_rows(
                conn, agent_id, sid, record, after_ordinal=cursor, limit=REBUILD_BATCH_ROWS
            )
        if not loaded:
            return last
        rows = [StoredRow(record, ordinal, _mapping(row)) for ordinal, row in loaded]
        _ = projector.feed(reader.parse_stored(rows))
        cursor = last = rows[-1].ordinal
        if len(loaded) < REBUILD_BATCH_ROWS:
            return last


async def _floor(
    pool: AsyncConnectionPool,
    agent_id: int,
    sid: str,
    record: str,
    after_ordinal: int,
    max_rows: int,
) -> int:
    async with pool.connection() as conn:
        floor = await aload_transcript_row_floor(
            conn, agent_id, sid, record, after_ordinal=after_ordinal, keep=max_rows
        )
    if floor is None:
        return after_ordinal
    logger.warning(
        "a turn has gone more than {limit} stored rows without a quiet point; the projector "
        "is rebuilt from the latest {limit} only, so the output of a call that began earlier "
        "is held for a bounded time and then shown top-level",
        event="harness_timeline_rebuild_bounded",
        agent_id=agent_id,
        record=record,
        limit=max_rows,
        rebuild_ordinal=after_ordinal,
        resumed_after=floor,
    )
    return floor
