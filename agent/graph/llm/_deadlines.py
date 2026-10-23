# meiva:note
# Deadline arithmetic of one streaming model request as values. The stream consumer reads the monotonic clock and asks these functions how long the next chunk may take and whether the attempt's total ceiling is spent.
# meiva:link agent/graph/llm/docs/llm.meiva.okf.md
from __future__ import annotations

from dataclasses import dataclass


# meiva:note
# The deadlines of one streaming attempt; `total_seconds` None leaves the attempt unbounded.
@dataclass(frozen=True)
class StreamBounds:
    ttft_seconds: float
    inter_chunk_seconds: float
    total_seconds: float | None


# meiva:note
# How long the next chunk may take, and whether the total ceiling is what bounds it.
@dataclass(frozen=True)
class ChunkWait:
    seconds: float
    total_is_bound: bool


def total_spent(bounds: StreamBounds, elapsed: float) -> bool:
    return bounds.total_seconds is not None and elapsed >= bounds.total_seconds


# meiva:note
# The first chunk (index 0) runs under the TTFT bound and later ones under the inter-chunk bound.
def stage_seconds(bounds: StreamBounds, chunk_idx: int) -> float:
    return bounds.ttft_seconds if chunk_idx == 0 else bounds.inter_chunk_seconds


# meiva:note
# The wait for chunk number `chunk_idx`, None once the total ceiling is spent. The remaining total wins when it is shorter than the stage bound.
def next_chunk_wait(bounds: StreamBounds, chunk_idx: int, elapsed: float) -> ChunkWait | None:
    stage = stage_seconds(bounds, chunk_idx)
    if bounds.total_seconds is None:
        return ChunkWait(stage, total_is_bound=False)
    remaining_total = bounds.total_seconds - elapsed
    if remaining_total <= 0:
        return None
    if remaining_total <= stage:
        return ChunkWait(remaining_total, total_is_bound=True)
    return ChunkWait(stage, total_is_bound=False)


def milliseconds_between(start: float, end: float) -> float:
    return (end - start) * 1000.0


# meiva:note
# The provider's generation window; None when no chunk arrived, because an empty stream has no honest decode window.
def decode_window_ms(first_ts: float | None, last_ts: float | None) -> float | None:
    if first_ts is None or last_ts is None:
        return None
    return milliseconds_between(first_ts, last_ts)
