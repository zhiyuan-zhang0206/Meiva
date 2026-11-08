# meiva:link base/agents/history/docs/range-read-contracts.meiva.okf.md
from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

import psycopg
from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.checkpoint.base import CheckpointTuple
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from base.agents.history.checkpoint import CheckpointReadError, FullHistory, single_segment_history
from base.agents.history.checkpoint_postgres_walks import HistoryPostgresSaver
from base.agents.history.checkpoint_serde import STATIC_CHECKPOINT_MSGPACK_TYPES
from base.agents.history.checkpoint_serializer import CheckpointSerializer
from base.agents.history.closing_request import METADATA_KEY, ClosingRequest
from base.agents.history.delta_read_compat import as_messages, reconstruct_delta_messages
from base.agents.history.harness_messages import (
    harness_timeline_to_full_history,
    load_harness_timeline_items,
)
from base.agents.messages.kwargs import read_meiva_kwargs
from base.db import Database
from base.db.rows import query_all
from base.host.boundary.untyped import object_mapping

SEGMENT_SHAPE_METADATA_KEY = "compact_segment_shape"
_MAX_SQL_END_INDEX = 1_000_000_000

_BOUNDARY_SHAPES_SQL = """
SELECT
    c.checkpoint_id,
    CASE WHEN valid.shape IS NOT NULL THEN (valid.shape->>'head')::int END AS head_count,
    CASE WHEN valid.shape IS NOT NULL THEN (valid.shape->>'body')::int END AS body_len,
    CASE
        WHEN valid.shape IS NOT NULL
         AND %s::int IS NOT NULL
         AND (%s::int - (valid.shape->>'head')::int) >= 1
        THEN valid.shape->'ids'->>(%s::int - (valid.shape->>'head')::int - 1)
    END AS end_id_at_index
FROM checkpoints AS c
LEFT JOIN LATERAL (
    SELECT
        CASE
            WHEN jsonb_typeof(c.metadata) = 'object'
             AND jsonb_typeof(c.metadata->'compact_segment_shape') = 'object'
            THEN (
                CASE
                    WHEN c.metadata->'compact_segment_shape'->>'v' = '1'
                     AND (c.metadata->'compact_segment_shape'->>'head') IN ('0', '1')
                     AND (c.metadata->'compact_segment_shape'->>'body') ~ '^[0-9]{1,9}$'
                     AND jsonb_typeof(c.metadata->'compact_segment_shape'->'ids') = 'array'
                    THEN (
                        CASE
                            WHEN jsonb_array_length(c.metadata->'compact_segment_shape'->'ids')
                                 = (c.metadata->'compact_segment_shape'->>'body')::int
                            THEN c.metadata->'compact_segment_shape'
                        END
                    )
                END
            )
        END AS shape
) AS valid ON true
WHERE c.thread_id = %s
  AND c.checkpoint_ns = ''
  AND c.metadata->>'compact_boundary' = 'true'
ORDER BY c.checkpoint_id ASC
"""

_STAMP_SEGMENT_SHAPE_SQL = """
UPDATE checkpoints
   SET metadata = jsonb_set(
       CASE WHEN jsonb_typeof(metadata) = 'object' THEN metadata ELSE '{}'::jsonb END,
       '{compact_segment_shape}',
       %s::jsonb,
       true
   )
 WHERE thread_id = %s
   AND checkpoint_ns = ''
   AND checkpoint_id = %s
"""


class BoundaryCheckpointGoneError(ValueError):
    pass


@dataclass(frozen=True)
class _BoundaryShapeRow:
    checkpoint_id: str
    head_count: int | None
    body_len: int | None
    end_id_at_index: str | None


@dataclass(frozen=True)
class BoundarySegmentShape:
    checkpoint_id: str
    head_count: int
    body_len: int
    end_id_at_index: str | None


@dataclass(frozen=True)
class SegmentMessages:
    checkpoint_id: str
    head: SystemMessage | None
    body: tuple[BaseMessage, ...]
    closing: ClosingRequest | None = None

    @property
    def head_count(self) -> int:
        return 1 if self.head is not None else 0

    @classmethod
    def from_messages(
        cls,
        checkpoint_id: str,
        messages: Sequence[BaseMessage],
        *,
        closing: ClosingRequest | None = None,
    ) -> SegmentMessages:
        if not messages:
            return cls(checkpoint_id=checkpoint_id, head=None, body=(), closing=closing)
        if isinstance(messages[0], SystemMessage):
            return cls(
                checkpoint_id=checkpoint_id,
                head=messages[0],
                body=tuple(messages[1:]),
                closing=closing,
            )
        return cls(checkpoint_id=checkpoint_id, head=None, body=tuple(messages), closing=closing)

    def shape_payload(self) -> dict[str, object]:
        return {
            "v": 1,
            "head": self.head_count,
            "body": len(self.body),
            "ids": [_shape_message_id(msg) for msg in self.body],
        }

    def end_id_at(self, end_index: int | None) -> str | None:
        if end_index is None:
            return None
        body_pos = end_index - self.head_count - 1
        if body_pos < 0 or body_pos >= len(self.body):
            return None
        return _shape_message_id(self.body[body_pos])


def _shape_message_id(message: BaseMessage) -> str | None:
    if read_meiva_kwargs(message).get("meiva_ephemeral_message_id") is True:
        return None
    msg_id = message.id
    if not isinstance(msg_id, str) or "\x00" in msg_id:
        return None
    return msg_id


class CheckpointSegmentReader:
    # meiva:note
    # Single-connection reader for one agent's checkpoints: caches any segment reconstructed in
    # this call and lazily stamps `compact_segment_shape` on sealed boundary rows so subsequent
    # range locations never replay prior boundary delta chains.
    # meiva:link base/agents/history/docs/range-read-contracts.meiva.okf.md

    def __init__(
        self,
        agent_id: int,
        conn: psycopg.Connection[dict[str, object]],
        saver: HistoryPostgresSaver,
    ) -> None:
        self._agent_id = agent_id
        self._thread_id = str(agent_id)
        self._conn = conn
        self._saver = saver
        self._segments: dict[str, SegmentMessages] = {}

    def read_latest(self) -> SegmentMessages | None:
        try:
            value = self._saver.get_tuple(
                {"configurable": {"thread_id": self._thread_id, "checkpoint_ns": ""}}
            )
            if value is None:
                return None
            checkpoint_id = str(value.checkpoint["id"])
            if checkpoint_id in self._segments:
                return self._segments[checkpoint_id]
            reconstruct_delta_messages(self._saver, value)
            segment = _segment_from_tuple(checkpoint_id, value)
        except CheckpointReadError:
            raise
        except Exception as exc:
            raise CheckpointReadError(
                f"latest checkpoint read failed for agent {self._agent_id}"
            ) from exc
        self._segments[checkpoint_id] = segment
        return segment

    def read_segment(self, checkpoint_id: str) -> SegmentMessages:
        if checkpoint_id in self._segments:
            return self._segments[checkpoint_id]
        try:
            value = self._saver.get_tuple(
                {
                    "configurable": {
                        "thread_id": self._thread_id,
                        "checkpoint_ns": "",
                        "checkpoint_id": checkpoint_id,
                    }
                }
            )
            if value is not None:
                reconstruct_delta_messages(self._saver, value)
                segment = _segment_from_tuple(checkpoint_id, value)
            else:
                segment = None
        except CheckpointReadError:
            raise
        except Exception as exc:
            raise CheckpointReadError(
                f"checkpoint {checkpoint_id} read failed for agent {self._agent_id}"
            ) from exc
        if segment is None:
            raise CheckpointReadError(
                f"checkpoint {checkpoint_id} missing for agent {self._agent_id}"
            )
        self._segments[checkpoint_id] = segment
        return segment

    def load_boundary_shapes(
        self,
        *,
        end_index: int | None = None,
        up_to_checkpoint_id: str | None = None,
    ) -> tuple[BoundarySegmentShape, ...]:
        sql_end = (
            end_index if end_index is not None and 0 <= end_index <= _MAX_SQL_END_INDEX else None
        )
        try:
            rows = query_all(
                self._conn,
                _BoundaryShapeRow,
                _BOUNDARY_SHAPES_SQL,
                (sql_end, sql_end, sql_end, self._thread_id),
            )
        except Exception as exc:
            raise CheckpointReadError(
                f"compaction boundary metadata read failed for agent {self._agent_id}"
            ) from exc
        if up_to_checkpoint_id is not None:
            stop = next(
                (i for i, row in enumerate(rows) if row.checkpoint_id == up_to_checkpoint_id),
                None,
            )
            if stop is None:
                raise BoundaryCheckpointGoneError(up_to_checkpoint_id)
            rows = rows[: stop + 1]
        shapes: list[BoundarySegmentShape] = []
        for row in rows:
            if row.head_count is not None and row.body_len is not None:
                shapes.append(
                    BoundarySegmentShape(
                        checkpoint_id=row.checkpoint_id,
                        head_count=row.head_count,
                        body_len=row.body_len,
                        end_id_at_index=row.end_id_at_index,
                    )
                )
                continue
            segment = self.read_segment(row.checkpoint_id)
            self._stamp_segment_shape(row.checkpoint_id, segment)
            shapes.append(
                BoundarySegmentShape(
                    checkpoint_id=row.checkpoint_id,
                    head_count=segment.head_count,
                    body_len=len(segment.body),
                    end_id_at_index=segment.end_id_at(end_index),
                )
            )
        return tuple(shapes)

    def _stamp_segment_shape(self, checkpoint_id: str, segment: SegmentMessages) -> None:
        try:
            self._conn.execute(
                _STAMP_SEGMENT_SHAPE_SQL,
                (Jsonb(segment.shape_payload()), self._thread_id, checkpoint_id),
            )
        except (psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction):
            return
        except Exception as exc:
            raise CheckpointReadError(
                f"checkpoint {checkpoint_id} shape stamp failed for agent {self._agent_id}"
            ) from exc


def _segment_from_tuple(checkpoint_id: str, value: CheckpointTuple) -> SegmentMessages:
    messages = as_messages(object_mapping(value.checkpoint["channel_values"]).get("messages", []))
    metadata: Mapping[str, object] = value.metadata
    return SegmentMessages.from_messages(
        checkpoint_id,
        messages,
        closing=ClosingRequest.from_metadata(metadata.get(METADATA_KEY)),
    )


@contextmanager
def open_segment_reader(db: Database, agent_id: int) -> Generator[CheckpointSegmentReader]:
    serde = CheckpointSerializer(allowed_msgpack_modules=STATIC_CHECKPOINT_MSGPACK_TYPES)
    pool = db.pool(autocommit=True, row_factory=dict_row)
    try:
        with ExitStack() as stack:
            try:
                conn = stack.enter_context(pool.connection())
            except Exception as exc:
                raise CheckpointReadError(
                    f"checkpoint connection failed for agent {agent_id}"
                ) from exc
            yield CheckpointSegmentReader(
                agent_id,
                conn,
                HistoryPostgresSaver(conn=conn, serde=serde),
            )
    finally:
        pool.close()


# meiva:note
# Read all persisted harness messages for an external harness agent as a FullHistory segment.
def read_harness_segment(db: Database, agent_id: int) -> FullHistory:
    with db.connect(autocommit=True) as conn:
        items = load_harness_timeline_items(conn, agent_id)
    return harness_timeline_to_full_history(items)


def read_checkpoint_segment(db: Database, agent_id: int, checkpoint_id: str) -> FullHistory:
    if checkpoint_id.startswith("h"):
        return read_harness_segment(db, agent_id)
    with open_segment_reader(db, agent_id) as reader:
        segment = reader.read_segment(checkpoint_id)
    messages = [
        *((segment.head,) if segment.head is not None else ()),
        *segment.body,
    ]
    history = single_segment_history(messages)
    return history._replace(segment_closings=(segment.closing,))
