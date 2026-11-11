# meiva:note
# The session vocabulary every external harness adapter names, apart from the session that runs.
# `BaseHarnessSession` owns asyncio tasks, the child probe and the registry, so it stays in
# `base_session` (a shell). What a strict module may name is here: the clock and config protocols,
# the close error, and the stream sentinel. Nothing here reads anything, and nothing here is
# async: `ActiveTurn` holds an event, so it stays with the session that runs turns.
from __future__ import annotations

from collections.abc import Coroutine
from datetime import datetime
from pathlib import Path
from typing import Protocol

__all__ = [
    "EVENT_STREAM_END",
    "SessionClock",
    "SessionClosedError",
    "SessionConfig",
]

EVENT_STREAM_END = object()


class SessionClosedError(RuntimeError):
    pass


class SessionClock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def asleep(self, seconds: float) -> Coroutine[object, object, None]: ...


class SessionConfig(Protocol):
    @property
    def cwd(self) -> Path: ...

    @property
    def model(self) -> str | None: ...

    @property
    def cancel_settle_seconds(self) -> float: ...

    @property
    def max_buffered_events(self) -> int: ...
