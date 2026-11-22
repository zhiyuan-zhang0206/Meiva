from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class DaemonSettingsLike(Protocol):
    @property
    def computer_use_lease_s(self) -> float: ...
    @property
    def computer_use_queue_timeout_s(self) -> float: ...
    @property
    def computer_use_session_idle_s(self) -> float: ...
    @property
    def computer_use_loop_stall_s(self) -> float: ...
    @property
    def computer_use_shutdown_drain_s(self) -> float: ...


@dataclass(frozen=True)
class ComputerUseConfig:
    computer_use_lease_s: float
    computer_use_queue_timeout_s: float
    computer_use_session_idle_s: float
    computer_use_loop_stall_s: float
    computer_use_shutdown_drain_s: float


def from_daemon_settings(source: DaemonSettingsLike) -> ComputerUseConfig:
    return ComputerUseConfig(
        computer_use_lease_s=float(source.computer_use_lease_s),
        computer_use_queue_timeout_s=float(source.computer_use_queue_timeout_s),
        computer_use_session_idle_s=float(source.computer_use_session_idle_s),
        computer_use_loop_stall_s=float(source.computer_use_loop_stall_s),
        computer_use_shutdown_drain_s=float(source.computer_use_shutdown_drain_s),
    )


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


DEFAULT_LEASE_S = 600.0
MIN_LEASE_SECONDS = 30
MAX_LEASE_SECONDS = 3600
LEASE_REMINDER_LEAD_SECONDS = 180.0
NO_LOCAL_DESKTOP_ERR = (
    "no local desktop available on this host; pass host='meiva://<machine>' "
    "or host='ssh://<alias>' (run `meiva computer list` to view available targets)"
)


def clamp_lease_ttl_seconds(raw: object, *, default_s: float) -> int | None:
    if raw is None:
        return max(MIN_LEASE_SECONDS, min(MAX_LEASE_SECONDS, round(default_s)))
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return None
    return max(MIN_LEASE_SECONDS, min(MAX_LEASE_SECONDS, round(raw)))


def finalize_release_result(
    local: tuple[int | None, dict[str, object], str, str | None],
    *,
    force: bool,
    remote: tuple[bool, str | None],
    holder: int | None,
    target_uri: str,
) -> tuple[str, str | None, dict[str, object] | None, str | None]:
    released, mouse, outcome, error = local
    remote_released, remote_error = remote
    if released is not None:
        body: dict[str, object] = {
            "released": released,
            "holder": holder,
            "host": target_uri,
            **mouse,
        }
        if remote_released:
            body["remote_released"] = True
        return outcome, error, body, None
    if force and remote_released:
        body = {
            "released": None,
            "remote_released": True,
            "holder": holder,
            "host": target_uri,
            **mouse,
        }
        return "ok", None, body, None
    if force and remote_error is not None:
        err = f"remote release failed: {remote_error}"
        return "error", err, None, err
    note = " (the mouse button you held was released)" if mouse.get("mouse_released") else ""
    return outcome, error, None, "not the screen holder" + note
