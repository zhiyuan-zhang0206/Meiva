from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ComputerUseConfig:
    computer_use_lease_s: float
    computer_use_queue_timeout_s: float
    computer_use_session_idle_s: float
    computer_use_loop_stall_s: float
    computer_use_shutdown_drain_s: float


# meiva:note
# Below this a misconfigured window would restart a healthy daemon.
_MIN_STALL_S = 1.0


# meiva:note
# The window actually used for a configured one: never below the floor.
def effective_stall_s(configured_s: float) -> float:
    return max(configured_s, _MIN_STALL_S)


# meiva:note
# How often the loop re-beats: the base cadence, shrunk to a third of a small window so an
# operator tuning the window down cannot trip a healthy loop.
def beat_interval_s(beat_s: float, stall_s: float) -> float:
    return min(beat_s, stall_s / 3)


# meiva:note
# Whether the loop has gone more than `stall_s` seconds without a beat.
def loop_is_silent(*, now: float, last_beat: float, stall_s: float) -> bool:
    return now - last_beat > stall_s
