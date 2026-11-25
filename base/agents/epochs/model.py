# meiva:link base/agents/epochs/docs/epochs.md

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

SwitchedBy = Literal["birth", "user", "self", "operator", "backfill"]


@dataclass(frozen=True, slots=True)
class HarnessEpoch:
    agent_id: int
    seq: int
    harness: str
    model: str | None
    session_id: str | None
    started_at: datetime
    ended_at: datetime | None
    source_start: int | None
    source_end: int | None
    switched_by: SwitchedBy
    reason: str | None


@dataclass(frozen=True, slots=True)
class RecordExtent:
    session_id: str
    offset: int


@dataclass(frozen=True, slots=True)
class SwitchExtents:
    leaving: RecordExtent | None = None
    arriving: RecordExtent | None = None


RecordExtentReader = Callable[[int, str], RecordExtent | None]
