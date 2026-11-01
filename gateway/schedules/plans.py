# meiva:note
# What the schedule API decides from a row and a request: values in, decisions out.
#
# The router reads the row and performs the write; whether an edit changed anything, whether a
# control action needs the manager to converge now, and which source answers a logs request are
# decided here.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, assert_never

__all__ = [
    "UPDATABLE",
    "ControlAction",
    "ControlPlan",
    "LogsSource",
    "capture_window",
    "changed_fields",
    "control_plan",
    "edit_needs_sync",
    "logs_view",
    "select_declared_secrets",
]

UPDATABLE = ("name", "description", "script", "command", "enabled", "machine")

# meiva:note
# A logs request always asks the session for at least this many lines, so a small window still
# fills the display.
_MIN_CAPTURE_LINES = 200

LogsSource = Literal["live", "transcript", "last_error", "none"]
ControlAction = Literal["start", "stop", "restart"]


# meiva:note
# What a start / stop / restart does to a schedule.
#
# ``refusal`` is the reason it is refused (the request is a conflict); otherwise ``enabled``
# is the value the row takes and ``needs_sync`` says whether the manager must converge the
# session now.
@dataclass(frozen=True)
class ControlPlan:
    refusal: str | None
    enabled: bool
    needs_sync: bool


# meiva:note
# The requested fields that differ from the row (columns: name, description, script,
# command, enabled, machine, in the router's full-row order).
def changed_fields(previous: tuple[object, ...], fields: Mapping[str, object]) -> dict[str, object]:
    current = dict(
        zip(
            UPDATABLE,
            (previous[1], previous[2], previous[11], previous[3], previous[4], previous[9]),
            strict=True,
        )
    )
    return {col: fields[col] for col in UPDATABLE if col in fields and fields[col] != current[col]}


# meiva:note
# Whether an edit must converge the session now: new code on an enabled schedule, a
# flipped enabled flag, or a reassigned machine.
def edit_needs_sync(changed: Mapping[str, object], *, enabled: bool) -> bool:
    code_changed = "script" in changed or "command" in changed
    return (code_changed and enabled) or "enabled" in changed or "machine" in changed


# meiva:note
# What `action` does to a schedule in its current state. The three actions are the whole type: a
# value outside them fails at once instead of being read as a start.
def control_plan(action: ControlAction, *, enabled: bool, status: str) -> ControlPlan:
    match action:
        case "start":
            wanted = True
        case "stop":
            wanted = False
        case "restart":
            if not enabled:
                return ControlPlan(
                    refusal="schedule is disabled; start it instead of restarting",
                    enabled=enabled,
                    needs_sync=False,
                )
            wanted = True
        case _:
            assert_never(action)
    needs_sync = (
        action == "restart" or enabled != wanted or (wanted and status in ("completed", "error"))
    )
    return ControlPlan(refusal=None, enabled=wanted, needs_sync=needs_sync)


# meiva:note
# How many lines of the session a logs request captures.
def capture_window(lines: int) -> int:
    return max(lines, _MIN_CAPTURE_LINES)


def _strip_trailing_blank(lines: list[str]) -> list[str]:
    stripped = list(lines)
    while stripped and not stripped[-1].strip():
        stripped.pop()
    return stripped


# meiva:note
# The schedule's recent output: a live session's capture, else its dead session's
# transcript, else the last crash traceback, else nothing. ``lines`` of 0 asks for all of it.
def logs_view(
    lines: int, *, captured: str | None, transcript: list[str] | None, last_error: str | None
) -> tuple[LogsSource, list[str]]:
    if captured is not None:
        tail = _strip_trailing_blank(captured.splitlines())
        return "live", tail[-lines:] if lines else tail
    if transcript:
        tail = _strip_trailing_blank(transcript)
        return "transcript", tail[-lines:] if lines else tail
    if last_error:
        return "last_error", last_error.splitlines()
    return "none", []


def select_declared_secrets(
    secret_names: tuple[str, ...],
    aliases: Mapping[str, str],
    process_env: Mapping[str, str],
) -> dict[str, str]:
    res: dict[str, str] = {}
    for k in secret_names:
        if k.startswith("MEIVA_"):
            continue
        val = aliases.get(k)
        if val is None:
            val = process_env.get(k)
        if val is not None:
            res[k] = val
    return res
