# meiva:note
# The schedule domain: what the schedule manager and the schedules themselves report.
#
# These lived in `root.py` because the root supervises schedule processes; they do not belong
# there. A schedule is its own subject — its sessions, rounds, relaunches and verification — and
# only `schedule_recovery_gated`, `schedule_launch_refused` and `schedule_process_lost` are the
# root-side half of it. Split out when a root-unit event pushed that module past its file budget
# (task #6403); the loader merges every module under this package, so nothing imports this one.

from __future__ import annotations

from typing import NotRequired, TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class ScheduleRecoveryGated(TypedDict):
    held_for_s: float


class ScheduleLaunchRefused(TypedDict):
    schedule_id: int
    reason: str


# meiva:note
# `schedule_process_lost` is the detection, and the only event that says a schedule's session is
# gone. The manager's session scan notices within its five-second tick and records the schedule as
# `error`; until this event existed that detection went only to a log line, and the earliest
# anything else spoke was `schedule_stalled` two hours later -- which a manual restart inside those
# two hours cancelled, so a lost session could pass entirely unreported (schedule 55, 2026-11-03:
# lost 16:31:09Z, restarted 18:16:38Z, the stall would have fired 18:31:09Z).
#
# The two events are two stages of one outage, not duplicates: this one says "it is down, act now",
# `schedule_stalled` says "it is still down two hours later, nobody has acted". When the relaunch
# was suppressed by the auto-restart budget, `schedule_auto_restart_suppressed` is emitted on the
# same tick and names that reason.
#
# No `not_live_since` here: the stall pass writes that column, and at detection it is still unset.
# The detection instant is this event's own timestamp.
class ScheduleProcessLost(TypedDict):
    schedule_id: int
    status: str


# meiva:note
# `schedule_stalled` fires once after an enabled, non-completed schedule has had nothing running it
# for more than two hours; a live observation rearms a later outage.
#
# ``evidence`` names the shape the manager saw: ``session_absent`` when no session exists at all,
# ``runner_gone`` when the session is still there as a shell whose runner died inside it -- and
# then ``runner_detail`` carries the process-table reading that proved it.
#
# ``silent_seconds`` measures the silence from the observable fact -- the last round the schedule
# itself finished -- so a manager that noticed hours late still reports how long the work has been
# stopped. ``session_missing_seconds`` is how long the manager has known; without a beat it is also
# the silence, and ``beat_written_at`` is absent.
class ScheduleStalled(TypedDict):
    schedule_id: int
    status: str
    silent_seconds: float
    session_missing_seconds: float
    evidence: str
    runner_detail: NotRequired[str]
    beat_written_at: NotRequired[str]


# meiva:note
# `schedule_not_progressing` payload — services/wake/schedule_manager/round_progress.py.
# Emitted once per episode when a resident schedule's session is still live but the round
# it promised (`alive_by` in its heartbeat) never started. A live session alone cannot say
# this: the loop writes a heartbeat every round, including rounds that found nothing to do,
# so a deadline that stops moving is the loop itself having stopped.
class ScheduleNotProgressing(TypedDict):
    schedule_id: int
    status: str
    overdue_seconds: float
    alive_by: str


# meiva:note
# `schedule_convergence_abandoned` payload — services/wake/schedule_manager/manager.py.
# Emitted once when the schedule manager stops retrying a schedule whose
# session survives its reap (or whose live session's launch provenance cannot
# be read) for ``blocked_seconds``. ``reason`` names the blocked step. The
# schedule is recorded as an error; an explicit new revision lifts the stop.
class ScheduleConvergenceAbandoned(TypedDict):
    schedule_id: int
    reason: str
    blocked_seconds: float


class ScheduleAutoRestart(TypedDict):
    schedule_id: int
    previous_generation: str
    current_generation: str
    restarts_in_window: int


class ScheduleAutoRestartSuppressed(TypedDict):
    schedule_id: int
    previous_generation: str
    current_generation: str
    restarts_in_window: int


# meiva:note
# `schedule_verify_failed` payload — cli/commands/management/schedules_verify.py.
# One row per non-clean `meiva schedules verify` sweep of the live table:
# ``red`` in-store scripts failed out of ``checked``; ``tool_error`` is set
# when the sweep itself could not run; ``detail`` lists the failures.
class ScheduleVerifyFailed(TypedDict):
    checked: int
    red: int
    tool_error: str | None
    detail: str


EVENTS: dict[str, EventSpec] = {
    "schedule_recovery_gated": telemetry_event(
        "schedule_recovery_gated",
        "the serving-receipt gate has refused schedule relaunches for longer than the configured window — the published receipt does not name the live root (a helper-reseeded root publishes none) and `meiva start` is needed; one event per episode",
        payload=ScheduleRecoveryGated,
        tier="anomaly",
        site="services/wake/schedule_manager/manager.py:_emit_gate_held",
    ),
    "schedule_launch_refused": telemetry_event(
        "schedule_launch_refused",
        "a schedule needed a (re)launch and was refused or did not take — once per schedule per gate episode, carrying the reason",
        payload=ScheduleLaunchRefused,
        tier="anomaly",
        site="services/wake/schedule_manager/manager.py:_emit_launch_refused",
    ),
    "schedule_process_lost": telemetry_event(
        "schedule_process_lost",
        "a schedule's session disappeared, or its session is a shell whose runner process is gone, and the manager recorded the schedule as an error — the detection itself, emitted once per loss at the moment it is seen; if nothing restarts the schedule, `schedule_stalled` follows two hours later as the second stage of the same outage",
        payload=ScheduleProcessLost,
        tier="anomaly",
        site="services/wake/schedule_manager/manager.py:_mark_lost emit_process_lost",
    ),
    "schedule_not_progressing": telemetry_event(
        "schedule_not_progressing",
        "a resident schedule's own next-round deadline passed while its session stayed live",
        payload=ScheduleNotProgressing,
        tier="anomaly",
        persist=True,
        site=("services/wake/schedule_manager/manager.py:_report_late_rounds telemetry.emit"),
    ),
    "schedule_stalled": telemetry_event(
        "schedule_stalled",
        "enabled non-completed schedule has had nothing running it for more than two hours — no session, or a session whose runner process is gone",
        payload=ScheduleStalled,
        tier="anomaly",
        site=("services/wake/schedule_manager/round_progress.py:report_stalled telemetry.emit"),
    ),
    "schedule_convergence_abandoned": telemetry_event(
        "schedule_convergence_abandoned",
        "the schedule manager stopped retrying a schedule whose session survives its reap or whose live session's launch provenance is unreadable, and recorded the schedule as an error; an explicit new revision lifts the stop",
        payload=ScheduleConvergenceAbandoned,
        tier="anomaly",
        site="services/wake/schedule_manager/manager.py:_give_up telemetry.emit",
    ),
    "schedule_auto_restart": telemetry_event(
        "schedule_auto_restart",
        "a schedule's session host was replaced (host reboot, deploy stop/start, pty-sessions service restart) and the manager relaunched the schedule once in the new generation; a second silent death inside the same generation stays a terminal error",
        payload=ScheduleAutoRestart,
        tier="anomaly",
        site="services/wake/schedule_manager/manager.py:_reclaim_after_host_replacement telemetry.emit",
    ),
    "schedule_auto_restart_suppressed": telemetry_event(
        "schedule_auto_restart_suppressed",
        "a schedule's session host was replaced again after the schedule already used the configured automatic relaunches of the rolling window; the schedule is recorded as an error and waits for an explicit restart",
        payload=ScheduleAutoRestartSuppressed,
        tier="anomaly",
        site="services/wake/schedule_manager/manager.py:_emit_auto_restart_suppressed telemetry.emit",
    ),
    "schedule_verify_failed": telemetry_event(
        "schedule_verify_failed",
        "meiva schedules verify found in-store schedule scripts that fail dry-import, an "
        "undefined-name read or call-signature binding, or the sweep itself failed",
        payload=ScheduleVerifyFailed,
        tier="anomaly",
        site="cli/commands/management/schedules_verify.py:_report_verify telemetry.emit",
    ),
}
