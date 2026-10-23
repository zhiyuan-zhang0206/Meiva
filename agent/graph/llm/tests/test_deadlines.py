from __future__ import annotations

from agent.graph.llm._deadlines import (
    ChunkWait,
    StreamBounds,
    decode_window_ms,
    milliseconds_between,
    next_chunk_wait,
    stage_seconds,
    total_spent,
)

_BOUNDS = StreamBounds(ttft_seconds=30.0, inter_chunk_seconds=10.0, total_seconds=100.0)
_UNBOUNDED = StreamBounds(ttft_seconds=30.0, inter_chunk_seconds=10.0, total_seconds=None)


def test_the_first_chunk_runs_under_ttft_and_later_ones_under_the_inter_chunk_bound() -> None:
    assert stage_seconds(_BOUNDS, 0) == 30.0
    assert [stage_seconds(_BOUNDS, idx) for idx in (1, 2, 50)] == [10.0, 10.0, 10.0]


def test_a_healthy_stream_waits_the_stage_bound_for_each_chunk() -> None:
    assert next_chunk_wait(_BOUNDS, 0, 0.0) == ChunkWait(30.0, False)
    assert next_chunk_wait(_BOUNDS, 3, 40.0) == ChunkWait(10.0, False)


def test_the_remaining_total_bounds_the_wait_once_it_is_shorter_than_the_stage() -> None:
    assert next_chunk_wait(_BOUNDS, 3, 95.0) == ChunkWait(5.0, True)
    assert next_chunk_wait(_BOUNDS, 3, 90.0) == ChunkWait(10.0, True)
    assert next_chunk_wait(_BOUNDS, 3, 89.9) == ChunkWait(10.0, False)


def test_a_spent_total_leaves_no_wait() -> None:
    assert next_chunk_wait(_BOUNDS, 3, 100.0) is None
    assert next_chunk_wait(_BOUNDS, 0, 250.0) is None


def test_no_total_ceiling_leaves_every_wait_to_the_stage_bound() -> None:
    assert next_chunk_wait(_UNBOUNDED, 0, 0.0) == ChunkWait(30.0, False)
    assert next_chunk_wait(_UNBOUNDED, 9, 10_000.0) == ChunkWait(10.0, False)
    assert not total_spent(_UNBOUNDED, 10**9)


def test_the_total_is_spent_at_the_ceiling() -> None:
    assert not total_spent(_BOUNDS, 99.99)
    assert total_spent(_BOUNDS, 100.0)


def test_the_decode_window_runs_from_the_first_chunk_to_the_last() -> None:
    assert decode_window_ms(1005.0, 1013.0) == 8000.0
    assert decode_window_ms(1005.0, 1005.0) == 0.0


def test_an_empty_stream_has_no_decode_window() -> None:
    assert decode_window_ms(None, None) is None


def test_latency_is_milliseconds_between_two_monotonic_readings() -> None:
    assert milliseconds_between(0.0, 13.0) == 13000.0
