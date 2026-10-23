# meiva:note
# What a node lifecycle decides, as values: whether its body gets a stall probe and when each
# half of it fires, and whether the enter snapshot renders the whole tail window or only what the
# agent's cursor has not covered. The shell in `_stall.py` and `_lifecycle.py` arms timers and
# publishes; a test asserts the plan.
# meiva:link agent/graph/docs/node-lifecycle.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# meiva:note
# The stall probe's tuning: the threshold after which a node body counts as stalled (0 disables
# the probe) and how long after the task dump the thread dump follows. The two writers share one
# stderr from different threads, so the stagger keeps their output from interleaving.
@dataclass(frozen=True)
class StallConfig:
    threshold_s: float
    thread_dump_stagger_s: float = 2.0


@dataclass(frozen=True)
class StallProbe:
    task_dump_after_s: float
    thread_dump_after_s: float


# meiva:note
# The claim node parks in the idle wait for hours and the harness turn waits on an external
# session, so arming the probe there would dump on every idle period. Nodes run one at a time,
# so the one process-global timer the probe uses never collides.
_PARKING_NODES = ("claim", "harness_turn")


# meiva:note
# The probe a node body gets: none below a positive threshold or for an exempt node, otherwise
# the task dump at the threshold and the thread dump one stagger later.
def stall_probe(node_name: str, config: StallConfig) -> StallProbe | None:
    if config.threshold_s <= 0 or node_name in _PARKING_NODES:
        return None
    return StallProbe(
        task_dump_after_s=config.threshold_s,
        thread_dump_after_s=config.threshold_s + config.thread_dump_stagger_s,
    )


class WindowMode(Enum):
    FULL = "full"
    INCREMENTAL = "incremental"


# meiva:note
# The first enter of a process (cursor 0), a history shrunk below the cursor (a compaction
# removed it) and an explicit full window all render the whole tail window; otherwise only the
# messages the cursor has not covered.
def window_mode(*, message_count: int, cursor: int, full_window: bool) -> WindowMode:
    if full_window or cursor == 0 or message_count < cursor:
        return WindowMode.FULL
    return WindowMode.INCREMENTAL
