# meiva:note
# The harness session's vocabulary behind one door: an adapter's strict modules name the clock and
# config protocols, the two queue/close errors and the stream sentinel from here, and never reach
# `base_session`, which owns the running session and is a shell. `turn.py` holds what a finished
# turn produced and what the session has spent: the two values every adapter builds when a turn
# settles.
from __future__ import annotations

from base.lm.harness.session.background import (
    BackgroundItem,
    BackgroundKind,
    BackgroundWork,
    BackgroundWorkReporting,
    LostBackgroundReporting,
    decode_items,
    describe,
    encode_items,
    lost_work_note,
)
from base.lm.harness.session.turn import SessionTotals
from base.lm.harness.session.vocabulary import (
    EVENT_STREAM_END,
    SessionClock,
    SessionClosedError,
    SessionConfig,
)

__all__ = [
    "EVENT_STREAM_END",
    "BackgroundItem",
    "BackgroundKind",
    "BackgroundWork",
    "BackgroundWorkReporting",
    "LostBackgroundReporting",
    "SessionClock",
    "SessionClosedError",
    "SessionConfig",
    "SessionTotals",
    "decode_items",
    "describe",
    "encode_items",
    "lost_work_note",
]
