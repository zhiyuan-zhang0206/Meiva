# meiva:note
# Which agents owe a periodic self-check, what counts as one of their rounds, and when a silence
# has lasted long enough to wake them, tell the lead, or escalate to the user.
#
# The watched list mirrors the role registry: one entry per agent whose duty is to notice things,
# with the cadence that duty owes. The cadence is a hand-set number with its reason written beside
# the entry, not a computed percentile: the input is a duty, and the measured gaps only tell you
# what an idle stretch looks like.
#
# Detection and the ladder are two scales, and both are counted from the round that first read the
# silence. Detection: no unexpired shell session left (a duty that keeps watchers has none once
# they were killed with the runtime -- the 2026-10-23 shape: 17 watchers gone, five hours of no
# monitoring, nothing said so), or no real turn for one and a half patrol intervals, or a schedule
# the agent owns that stopped firing on the cadence it had been keeping.
#
# A counts unexpired declarations, and a declaration outlives its session: the TTL row is only
# removed when the reaper reaches its expiry or the agent is terminated, so a session killed with a
# host restart leaves its row behind until a date it declared weeks earlier (measured 2026-10-27:
# 934 of one duty's 984 rows had no live session). A is therefore a floor, not a census -- it fires
# when a fleet is wiped and gone from the table, not when its rows merely outnumber its shells.
#
# A declared heartbeat pause is not silence. Every watched duty spends hours dormant on purpose
# (measured over the seven days to 2026-10-27: 312 142 pauses / 492 h, 6111 34 / 175 h, 1818 29 /
# 152 h, averaging five hours each), so reading dormancy as silence would wake an idle agent on
# every cycle -- the opposite of asking whether it is still taking its rounds. The pause suppresses
# only the turn-age condition: a fleet of dead sessions is still a real loss while its owner naps.
#
# The fourth signal in the accepted design -- the age of a per-round ledger row -- has no writer
# in this cluster yet. Rather than read something adjacent and call it the ledger, that signal is
# simply not read, and every round prints the signals it could not measure as `blind=...`. A blind
# signal is never counted as healthy, and a round never reports "all well" from values it did not
# read.
# meiva:link schedules/README.md

from __future__ import annotations

import statistics
from itertools import pairwise
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

__all__ = [
    "ESCALATE_MINUTES",
    "MINUTE",
    "Action",
    "Observation",
    "RoundPlan",
    "ScheduleVerdict",
    "WatchState",
    "WatchedAgent",
    "judge_schedule",
    "plan_round",
    "repeat_seconds",
    "silent_reasons",
    "watched_agents",
]

# meiva:note
# Detection: a turn older than `WAKE_MULTIPLE` patrol intervals counts as no round. Once a silence
# is detected the wake goes out at once; the lead hears about it after `REPORT_MULTIPLE` intervals;
# the user hears about it four hours after that, and never before the lead has been told, so the
# escalation cannot overtake the report it is escalating. For the hourly duty the 2026-10-23 report
# fixed, that reads: detect at 90 minutes, report at 180, escalate at 240 -- for a longer duty every
# rung moves out with its own cadence.
MINUTE = 60.0
WAKE_MULTIPLE = 1.5
REPORT_MULTIPLE = 3.0
ESCALATE_MINUTES = 240.0
SCHEDULE_LATE_MULTIPLE = 3.0
SCHEDULE_MIN_FIRES = 5


# meiva:note
# How often a rung may repeat while the silence lasts. A stuck duty must not cost a message an
# hour for a day: the first hour repeats hourly, the stretch between the report and the escalation
# repeats every four hours, and after the escalation every twelve. Each rung keeps its own clock,
# and a silence that recovers clears all three.
FIRST_REPEAT_SECONDS = 3600.0
REPORT_REPEAT_SECONDS = 4 * 3600.0
ESCALATED_REPEAT_SECONDS = 12 * 3600.0

_TERMINAL = {"terminated", "missing"}


# meiva:note
# One duty: the agent, what it owes, and the schedules whose firing is its evidence. `keeps_sessions`
# is false for a duty that holds no shell sessions by design, so "zero sessions" cannot be read as
# "its watchers were killed" there.
@dataclass(frozen=True)
class WatchedAgent:
    agent_id: int
    duty: str
    patrol_minutes: float
    keeps_sessions: bool
    schedules: tuple[str, ...] = ()


# meiva:note
# The watched duties, with the cadence each owes and why. The three cadences are hand-set: 312's
# wake-up gaps reach 53 minutes at the 90th percentile and five hours overnight, so two hours keeps
# the turn-age check out of its night; 6111's reach 9 minutes, and an hour is slack for a working
# day; 1818's rounds are event-driven, so four hours is an ordinary quiet stretch and six is not.
# Kept as a function so importing this module builds no objects: the schedule process imports it to
# run a round and never to enumerate duties at import time. A new duty is one entry here plus its
# cadence; nothing in the schedule template changes.
def watched_agents() -> tuple[WatchedAgent, ...]:
    return (
        WatchedAgent(
            agent_id=312,
            duty="machine resource monitoring (swap, disk, load) across the fleet",
            patrol_minutes=120.0,
            keeps_sessions=True,
        ),
        WatchedAgent(
            agent_id=6111,
            duty="Monsora SRE: preview cluster, host firewall, session recovery",
            patrol_minutes=60.0,
            keeps_sessions=True,
        ),
        WatchedAgent(
            agent_id=1818,
            duty="cluster operation: rollout, alerting, disk and database floor",
            patrol_minutes=240.0,
            keeps_sessions=True,
            schedules=("trace-ship-tempo",),
        ),
    )


# meiva:note
# What one round read about one agent. `status` is the lifecycle status the rotation table holds:
# a terminated agent is not silent, it is finished, and waking it would resurrect a duty the user
# ended on purpose. `paused_minutes` is the declared heartbeat pause still running, if any.
@dataclass(frozen=True)
class Observation:
    status: str
    sessions: int
    active_minutes: float | None
    paused_minutes: float | None = None
    late_schedules: tuple[str, ...] = ()
    blind: tuple[str, ...] = ()

    @property
    def paused(self) -> bool:
        return self.paused_minutes is not None


# meiva:note
# What the sentinel carries per agent between rounds. A zero `last_*` means "this rung never fired
# for this agent", which is not the same as "its cool-down has not elapsed"; reading the two as one
# makes the first message depend on how large the clock happens to be.
@dataclass(frozen=True)
class WatchState:
    silent_since: float | None = None
    last_wake_at: float = 0.0
    last_report_at: float = 0.0
    last_escalate_at: float = 0.0


# meiva:note
# One message the round will send: whom, which rung, and the text.
@dataclass(frozen=True)
class Action:
    target: int
    kind: Literal["wake", "report", "escalate"]
    text: str


# meiva:note
# One agent's outcome for this round: the state to keep, why it counted as silent (empty when it
# did not), the messages to send, and the lines the round prints.
@dataclass(frozen=True)
class RoundPlan:
    state: WatchState
    silent_minutes: float
    reasons: tuple[str, ...]
    actions: tuple[Action, ...]
    log: tuple[str, ...]


# meiva:note
# Whether a schedule is on its own cadence, late, or unmeasurable. At most one of the two reasons
# is set; both empty means on time.
@dataclass(frozen=True)
class ScheduleVerdict:
    late: str | None = None
    blind: str | None = None


# meiva:note
# A cadence needs at least `SCHEDULE_MIN_FIRES` fires: two points are a line, not a rhythm, and
# calling a barely-started schedule late would wake its owner for nothing. The order of `fires` is
# the caller's; the median gap is read oldest to newest and the wait from the newest fire.
def judge_schedule(name: str, fires: tuple[datetime, ...], *, now: datetime) -> ScheduleVerdict:
    if len(fires) < SCHEDULE_MIN_FIRES:
        return ScheduleVerdict(blind=f"{name}: {len(fires)} fires, no cadence")
    ordered = sorted(fires)
    gaps = sorted(
        (later - earlier).total_seconds() / MINUTE for earlier, later in pairwise(ordered)
    )
    median = statistics.median(gaps)
    if median <= 0:
        return ScheduleVerdict(blind=f"{name}: overlapping fires, no cadence")
    age = (now - ordered[-1]).total_seconds() / MINUTE
    if age > SCHEDULE_LATE_MULTIPLE * median:
        return ScheduleVerdict(late=f"{name}: last fire {age:.0f}m ago, cadence {median:.0f}m")
    return ScheduleVerdict()


# meiva:note
# Why this observation counts as silence. An agent that is terminated or absent is not silent; a
# missing `last_active_at` is not silence either, it is a reading the round could not take and is
# reported as blind by the caller. A declared pause removes the turn-age condition and nothing
# else: its sessions are still expected to be there when it wakes.
def silent_reasons(entry: WatchedAgent, observation: Observation) -> tuple[str, ...]:
    if observation.status in _TERMINAL:
        return ()
    reasons: list[str] = []
    if entry.keeps_sessions and observation.sessions == 0:
        reasons.append("sessions=0")
    if (
        not observation.paused
        and observation.active_minutes is not None
        and observation.active_minutes > WAKE_MULTIPLE * entry.patrol_minutes
    ):
        reasons.append(f"active={observation.active_minutes:.0f}m")
    if observation.late_schedules:
        reasons.append("late=" + "; ".join(observation.late_schedules))
    return tuple(reasons)


def _due(last: float, now: float, cooldown: float) -> bool:
    return last == 0.0 or now - last >= cooldown


# meiva:note
# The stretch between messages, chosen by which rung the silence has already passed: hourly while
# the lead has not been told, four-hourly between the report and the escalation, and twelve-hourly
# after it. One stuck duty therefore costs a handful of messages a day, not one an hour.
def repeat_seconds(state: WatchState) -> float:
    if state.last_escalate_at != 0.0:
        return ESCALATED_REPEAT_SECONDS
    if state.last_report_at != 0.0:
        return REPORT_REPEAT_SECONDS
    return FIRST_REPEAT_SECONDS


def _raw(observation: Observation) -> str:
    active = (
        "unknown" if observation.active_minutes is None else f"{observation.active_minutes:.0f}m"
    )
    paused = (
        "" if observation.paused_minutes is None else f" paused={observation.paused_minutes:.0f}m"
    )
    return f"A(sessions)={observation.sessions} B(ledger)=not read C(active)={active}{paused}"


def _blind(observation: Observation) -> str:
    return "blind: " + "; ".join(observation.blind) if observation.blind else ""


# meiva:note
# One round for one agent. Silence starts at the first round that reads it, so a sentinel that was
# itself down does not invent hours of silence it never saw; recovery clears the whole state, so
# the next silence starts its ladder from the first rung. Each rung is gated on the one below it:
# no escalation before the lead has been told, and no report before the agent has been woken.
def plan_round(
    entry: WatchedAgent,
    observation: Observation,
    state: WatchState,
    *,
    now: float,
    stamp: str,
    report_agent: int,
) -> RoundPlan:
    if observation.status in _TERMINAL:
        return RoundPlan(
            WatchState(), 0.0, (), (), (f"{entry.agent_id}: skipped status={observation.status}",)
        )

    reasons = silent_reasons(entry, observation)
    if not reasons:
        recovered = (
            ""
            if state.silent_since is None
            else f" recovered after {(now - state.silent_since) / MINUTE:.0f}m"
        )
        blind = f" {_blind(observation)}" if observation.blind else ""
        return RoundPlan(
            WatchState(), 0.0, (), (), (f"{entry.agent_id}: alive{recovered}.{blind}",)
        )

    silent_since = state.silent_since if state.silent_since is not None else now
    silent_minutes = (now - silent_since) / MINUTE
    since = datetime.fromtimestamp(silent_since, UTC).isoformat(timespec="minutes")
    ladder = "" if silent_minutes < 1 else f", {silent_minutes:.0f} min into the ladder"
    detail = (
        f"{entry.agent_id} silent: {', '.join(reasons)} (first detected {since}{ladder}) | "
        f"{_raw(observation)}"
    )
    blind = f" | {_blind(observation)}" if observation.blind else ""
    repeat = repeat_seconds(state)
    actions: list[Action] = []
    if _due(state.last_wake_at, now, repeat):
        state = replace(state, last_wake_at=now)
        actions.append(
            Action(
                entry.agent_id,
                "wake",
                f"[agent-watch] {detail}{blind}. Duty: {entry.duty}. "
                "Reply only if you cannot resume on your own; this message repeats on a widening "
                "interval and exists so a duty that stopped taking its rounds is rebuilt, not "
                "discovered hours later.",
            )
        )
    if silent_minutes >= REPORT_MULTIPLE * entry.patrol_minutes and _due(
        state.last_report_at, now, repeat
    ):
        state = replace(state, last_report_at=now)
        actions.append(
            Action(
                report_agent,
                "report",
                f"[agent-watch] {detail}{blind}. The agent has been woken at least once and stayed "
                f"silent past its report interval. Duty: {entry.duty}.",
            )
        )
    escalate_at = max(ESCALATE_MINUTES, REPORT_MULTIPLE * entry.patrol_minutes)
    if (
        state.last_report_at != 0.0
        and silent_minutes >= escalate_at
        and _due(state.last_escalate_at, now, repeat)
    ):
        state = replace(state, last_escalate_at=now)
        actions.append(
            Action(
                report_agent,
                "escalate",
                f"[agent-watch][escalate-to-user] {detail}{blind}. Duty: {entry.duty}. The lead was "
                "told and the agent stayed silent past the escalation interval: this is the rung "
                "that needs the user told, in the lead's words, not another wake.",
            )
        )
    state = replace(state, silent_since=silent_since)
    log = (f"{entry.agent_id}: silent {silent_minutes:.0f}m ({', '.join(reasons)}){blind}",)
    return RoundPlan(state, silent_minutes, reasons, tuple(actions), log)
