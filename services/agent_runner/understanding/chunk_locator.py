# meiva:note
# Single-segment chunk location for the understanding consumer: reads `compact_segment_shape`
# metadata on sealed boundaries and reconstructs only the one checkpoint segment that holds the
# claimed chunk.
# meiva:link base/agents/history/docs/range-read-contracts.meiva.okf.md
# meiva:link base/agents/history/hierarchy/docs/chunks.meiva.okf.md

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from base.agents.history.checkpoint_segment import (
    BoundarySegmentShape,
    CheckpointSegmentReader,
    SegmentMessages,
    open_segment_reader,
)
from base.agents.history.hierarchy.chunks import (
    Chunk,
    ChunkDriftError,
    ChunkJob,
    ChunkNotReadyError,
    LocatedChunk,
    locate_in_segment,
)
from base.db import Database


def _segment_starts(boundaries: Sequence[BoundarySegmentShape]) -> tuple[int, ...]:
    if not boundaries:
        return ()
    starts = [boundaries[0].head_count]
    for shape in boundaries[:-1]:
        starts.append(starts[-1] + shape.body_len)
    return tuple(starts)


def _drift_error(job: ChunkJob) -> ChunkDriftError:
    return ChunkDriftError(
        f"no segment holds message {job.end_msg_id!r} at index {job.end_index - 1}"
    )


def _not_ready_error(job: ChunkJob) -> ChunkNotReadyError:
    return ChunkNotReadyError(f"the newest segment is shorter than index {job.end_index}")


def _locate_closing(
    reader: CheckpointSegmentReader, job: ChunkJob, boundary_id: str
) -> LocatedChunk:
    boundaries = reader.load_boundary_shapes(up_to_checkpoint_id=boundary_id)
    segment_index = len(boundaries) - 1
    segment = reader.read_segment(boundary_id)
    found = locate_in_segment(
        segment.head,
        segment.body,
        Chunk(job.start_index, job.end_index),
        job.end_msg_id,
        segment=segment_index,
        segment_start=_segment_starts(boundaries)[segment_index],
        closing=True,
    )
    if isinstance(found, LocatedChunk):
        return found
    raise _drift_error(job)


def _locate_uncompacted(latest: SegmentMessages, job: ChunkJob) -> LocatedChunk:
    if latest.head is None and not latest.body:
        raise _drift_error(job)
    found = locate_in_segment(
        latest.head,
        latest.body,
        Chunk(job.start_index, job.end_index),
        job.end_msg_id,
        segment=0,
        segment_start=latest.head_count,
        closing=False,
    )
    if found == "not_ready":
        raise _not_ready_error(job)
    if found is not None:
        return found
    raise _drift_error(job)


def _locate_in_boundaries(
    reader: CheckpointSegmentReader,
    boundaries: Sequence[BoundarySegmentShape],
    starts: Sequence[int],
    job: ChunkJob,
    *,
    latest_is_boundary: bool,
) -> LocatedChunk | Literal["not_ready"] | None:
    chunk = Chunk(job.start_index, job.end_index)
    not_ready = False
    for k in range(len(boundaries) - 1, -1, -1):
        shape = boundaries[k]
        end_body = job.end_index - shape.head_count
        if shape.body_len < end_body:
            not_ready = not_ready or (latest_is_boundary and k == len(boundaries) - 1)
            continue
        if end_body < 1 or shape.end_id_at_index != job.end_msg_id:
            continue
        segment = reader.read_segment(shape.checkpoint_id)
        found = locate_in_segment(
            segment.head,
            segment.body,
            chunk,
            job.end_msg_id,
            segment=k,
            segment_start=starts[k],
            closing=False,
        )
        if isinstance(found, LocatedChunk):
            return found
    return "not_ready" if not_ready else None


def _locate_live(reader: CheckpointSegmentReader, job: ChunkJob) -> LocatedChunk:
    latest = reader.read_latest()
    if latest is None:
        raise _drift_error(job)
    boundaries = reader.load_boundary_shapes(end_index=job.end_index)
    if not boundaries:
        return _locate_uncompacted(latest, job)
    starts = _segment_starts(boundaries)
    latest_is_boundary = any(b.checkpoint_id == latest.checkpoint_id for b in boundaries)
    not_ready = False
    if not latest_is_boundary:
        open_found = locate_in_segment(
            latest.head,
            latest.body,
            Chunk(job.start_index, job.end_index),
            job.end_msg_id,
            segment=len(boundaries),
            segment_start=starts[-1] + boundaries[-1].body_len,
            closing=False,
        )
        if isinstance(open_found, LocatedChunk):
            return open_found
        not_ready = open_found == "not_ready"
    boundary_found = _locate_in_boundaries(
        reader,
        boundaries,
        starts,
        job,
        latest_is_boundary=latest_is_boundary,
    )
    if isinstance(boundary_found, LocatedChunk):
        return boundary_found
    if not_ready or boundary_found == "not_ready":
        raise _not_ready_error(job)
    raise _drift_error(job)


def locate_job_chunk(db: Database, job: ChunkJob) -> LocatedChunk:
    with open_segment_reader(db, job.agent_id) as reader:
        if job.boundary_checkpoint_id is not None:
            return _locate_closing(reader, job, job.boundary_checkpoint_id)
        return _locate_live(reader, job)
