from __future__ import annotations

from collections.abc import Awaitable, Sequence
from enum import StrEnum
from typing import Protocol

from base.agents.history.timeline_item import TimelineItem
from base.lm.harness.events import Usage
from base.lm.harness.session.transcript.drafts import SentInput

# meiva:note
# What a graph invocation asks of the session-scoped timeline writer of an external agent whose timeline is
# projected from the CLI's own transcript (`agent/harness_timeline/`, composed by the agent host). The writer
# outlives every invocation: the invocation hands it what it sent (`register_sent`, before the hand-over), asks
# it to read now and waits for the turn's closing entry (`wait_for_next_turn_end`), and asks which inputs the record
# has not yet shown (`undelivered`). It never hands the writer content: what the timeline shows comes from the
# record. A new writer holds its reads until an invocation has registered the inputs an earlier process left
# claimed and calls `start_reading` (a row the CLI wrote for an input nobody registered would be read as unmatched
# and the input could then never be settled). The context carries one only while the agent's `harness_transcript_timeline` setting is on; a
# context without one runs the adapter-event path unchanged.
# meiva:link agent/harness_timeline/README.md


class TurnEndWait(StrEnum):
    CLOSED = "closed"
    QUIET = "quiet"
    CEILING = "ceiling"


class TimelineSession(Protocol):
    @property
    def items_written(self) -> int: ...

    @property
    def fault(self) -> Exception | None: ...

    def take_closed_usage(self) -> Usage | None: ...

    def register_input(
        self,
        key: str,
        text: str,
        parts: Sequence[tuple[str, TimelineItem]],
        inbound_ids: tuple[int, ...],
    ) -> None: ...

    def forget_sent(self, key: str) -> None: ...

    def undelivered(self) -> tuple[SentInput, ...]: ...

    def take_invalid_reply(self) -> bool: ...

    def start_reading(self) -> None: ...

    def read_now(self) -> Awaitable[bool]: ...

    def wait_for_next_turn_end(
        self, *, quiet_seconds: float, ceiling_seconds: float
    ) -> Awaitable[TurnEndWait]: ...

    def write_marker(self, text: str, *, error: bool) -> Awaitable[None]: ...
