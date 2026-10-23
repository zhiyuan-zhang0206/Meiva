# meiva:note
# The decisions behind waiting for a checkpoint to hold a state's last message, as values. The shell in understanding_chunks.py reads the checkpoint table and the clock and sleeps; these functions say whether the checkpoint has caught up, whether to keep waiting, and when the last message was read.
# meiva:link agent/hooks/docs/hooks.meiva.okf.md
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from langchain_core.messages import AnyMessage

from base.agents.messages.kwargs import message_read_time

SnapshotVerdict = Literal["caught_up", "wait", "give_up"]


# meiva:note
# How long a compaction waits for the checkpoint to hold the state's last message before it stamps the boundary anyway: the graph persists a super-step's checkpoint asynchronously, so the newest row can lag the state by one step.
@dataclass(frozen=True)
class SnapshotWait:
    wait_seconds: float = 5.0
    poll_seconds: float = 0.5


# meiva:note
# When the model read `message` (`meiva_picked_up_at`, else `meiva_created_at`) as an aware time; None for a missing, unparsable or timezone-less stamp (an old message).
def read_time(message: AnyMessage) -> datetime | None:
    stamp = message_read_time(message)
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# meiva:note
# A checkpoint is written after the messages of its step, so one whose time is not before the last message's read time holds it. Past the wait it is given up on.
def snapshot_verdict(
    newest: datetime | None, last_read: datetime, elapsed: float, wait: SnapshotWait
) -> SnapshotVerdict:
    if newest is not None and newest >= last_read:
        return "caught_up"
    if elapsed >= wait.wait_seconds:
        return "give_up"
    return "wait"
