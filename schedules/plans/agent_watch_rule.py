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

import re
import statistics
from collections.abc import Iterable, Mapping
from itertools import pairwise
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

__all__ = [
    "ESCALATE_MINUTES",
    "MINUTE",
    "SEAT_COOLDOWN_SECONDS",
    "SEAT_REGISTRY_VERSION",
    "SEAT_SETTLE_SECONDS",
    "Action",
    "Observation",
    "RoundPlan",
    "ScheduleVerdict",
    "SeatReadings",
    "SeatRegistry",
    "SeatRegistryError",
    "SeatState",
    "SeatWake",
    "WatchState",
    "WatchedAgent",
    "judge_schedule",
    "plan_round",
    "plan_seats",
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


# meiva:note
# Seats: what this host owes each agent, read against the sessions it is running.
#
# A duty's cadence says whether its owner is still taking rounds. It cannot say whether one of the
# fifteen watchers that owner keeps is gone: a count of live sessions reads the same at fifteen and
# at fourteen. So each agent keeps its own registry of named seats (`MEIVA_HOME/state/
# seat-registry/<agent>.json`), the backend names a session after the seat, and a round compares the
# two by name.
#
# Two readings stay with their owners. This module judges **presence** only: a seat's script, its
# cadence, its health rule and its rebuild belong to the agent that wrote the registry. Whether a
# running seat is *advancing* is a different finding from a different reader, and neither may be
# reported as the other. The registry's `version` is a refusal rather than a hint: a newer owner may
# mean something else by a seat, and rebuilding the wrong thing on a guess is worse than a blind
# line. The whole judgement is one public call (`plan_seats`); a registry it cannot read becomes a
# `seats-blind=` note for that owner, never a silent skip.
SEAT_REGISTRY_VERSION = 1

# meiva:note
# One wake per absence, not one per round: the same absent set inside the cool-down stays quiet,
# while a *different* set wakes at once -- a second seat dying is news one minute after the first
# wake. Thirty minutes is six rounds of this schedule.
SEAT_COOLDOWN_SECONDS = 1800.0

# meiva:note
# How long after the tree's own start a missing seat is read as "the start is still finishing". A
# wake during that window would send the owner to re-arm into a runtime that is about to launch its
# own recovery, and it is the only window in which a wake is withheld: a missing seat during a stop
# or an update is exactly what the owner needs to hear, so `intent` never suppresses this wake.
SEAT_SETTLE_SECONDS = 300.0

# meiva:note
# The backend names an agent's session `meiva-agent-<agent>-shell-<session>[-<label>]`, and the label
# is the name the agent gave the seat. `base/sessions/coding_session_owner_record.py` composes the
# same shape for coding-tool sessions and `ops/cluster_status/model.py` reads it back with its own
# expression; a test builds names through the base composer and parses them here, so a change to the
# shape fails that test instead of reading every seat as absent.
_SESSION_RE = re.compile(r"-agent-(\d+)-shell-(\d+)(?:-(.+))?$")


# meiva:note
# A registry this round cannot read as the version it knows.
class SeatRegistryError(ValueError):
    pass


# meiva:note
# What one agent's registry says this host owes it.
@dataclass(frozen=True, slots=True)
class SeatRegistry:
    agent_id: int
    seats: tuple[str, ...]


# meiva:note
# What the last round said about one agent, so the cool-down survives a restart.
@dataclass(frozen=True, slots=True)
class SeatState:
    absent: tuple[str, ...] = ()
    woke_at: float | None = None


@dataclass(frozen=True, slots=True)
class SeatWake:
    agent_id: int
    absent: tuple[str, ...]
    text: str


# meiva:note
# One round's readings about the seats, grouped so the decision takes one value: the host's session
# names as the backend listed them, what the database says each owner is doing, what the last round
# remembered, and the two policy numbers the decision is made against.
@dataclass(frozen=True, slots=True)
class SeatReadings:
    sessions: Iterable[str]
    statuses: Mapping[int, str]
    states: Mapping[int, SeatState]
    now: float
    cooldown_s: float
    registry_dir: str


# meiva:note
# Everything beyond `version` and the seat names belongs to the other readers (script, cadence,
# health rule), so it is ignored rather than validated here: a field this module does not read
# cannot make a registry unreadable.
def _parse_seat_registry(agent_id: int, payload: object) -> SeatRegistry:
    if not isinstance(payload, Mapping):
        raise SeatRegistryError("registry is not an object")
    version = payload.get("version")
    if version != SEAT_REGISTRY_VERSION:
        raise SeatRegistryError(f"registry version {version!r} is not {SEAT_REGISTRY_VERSION}")
    raw = payload.get("seats")
    if not isinstance(raw, list):
        raise SeatRegistryError("registry has no seats list")
    names: list[str] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise SeatRegistryError("seat entry is not an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise SeatRegistryError("seat entry has no name")
        if name in names:
            raise SeatRegistryError(f"seat {name!r} appears twice")
        names.append(name)
    return SeatRegistry(agent_id, tuple(names))


# meiva:note
# Who owns a session, and under which seat name. Three answers, and only the last is a seat: a name
# that is not an agent shell session at all (`meiva-agent-<id>`, a service session) answers None; an
# agent's shell without a label answers a None label; a labelled one answers its name.
def _seat_owner(session_name: str) -> tuple[int, str | None] | None:
    match = _SESSION_RE.search(session_name)
    if match is None:
        return None
    return int(match.group(1)), match.group(3)


# meiva:note
# The seat names each agent currently has, from one listing of the host's sessions.
def _live_seat_names(session_names: Iterable[str]) -> dict[int, frozenset[str]]:
    owned: dict[int, set[str]] = {}
    for name in session_names:
        owner = _seat_owner(name)
        if owner is None:
            continue
        agent_id, label = owner
        if label is None:
            continue
        owned.setdefault(agent_id, set()).add(label)
    return {agent_id: frozenset(names) for agent_id, names in owned.items()}


# meiva:note
# The registry's seats this host does not have, in registry order.
def _absent_seats(registry: SeatRegistry, live: Mapping[int, frozenset[str]]) -> tuple[str, ...]:
    present = live.get(registry.agent_id, frozenset())
    return tuple(name for name in registry.seats if name not in present)


def _seat_wake_wanted(
    absent: Iterable[str],
    *,
    previous: Iterable[str] | None,
    woke_at: float | None,
    now: float,
    cooldown_s: float,
) -> bool:
    current = tuple(sorted(absent))
    if not current:
        return False
    if previous is None or tuple(sorted(previous)) != current:
        return True
    return woke_at is None or now - woke_at >= cooldown_s


# meiva:note
# The message that owns the absence. It names the seats and the file, never a cause.
def _seat_wake_text(agent_id: int, absent: Iterable[str], *, registry_path: str) -> str:
    names = tuple(absent)
    return (
        f"[seat-ensure] {len(names)} of your seats are not running on this host: "
        f"{', '.join(names)}. Registry: {registry_path} (agent {agent_id}). "
        "Put them back the way you normally re-arm them; the stop that took them was the intended "
        "one, so nothing else will restore them. This repeats only after 30 minutes or when a "
        "different seat goes missing."
    )


# meiva:note
# The round's decision, as values: who to wake, what to remember, and what could not be read. A
# terminated agent is finished rather than absent -- its registry stops owing anything the moment
# the user ended it, and a wake would resurrect a duty that was deliberately closed. An agent whose
# row this round could not read is left alone for the same reason a blind session listing wakes
# nobody. `registries` carries each owner's raw file payload: validation happens here, at the one
# boundary, so a registry written by a newer owner becomes a note instead of a guess.
def plan_seats(
    registries: Iterable[tuple[int, object]], readings: SeatReadings
) -> tuple[tuple[SeatWake, ...], dict[int, SeatState], tuple[str, ...]]:
    live = _live_seat_names(readings.sessions)
    wakes: list[SeatWake] = []
    next_states: dict[int, SeatState] = {}
    notes: list[str] = []
    for agent_id, payload in registries:
        try:
            registry = _parse_seat_registry(agent_id, payload)
        except SeatRegistryError:
            notes.append(f"{agent_id}: seats-blind=registry")
            continue
        missing = _absent_seats(registry, live)
        if not missing:
            continue
        previous = readings.states.get(agent_id)
        status = readings.statuses.get(agent_id)
        if status == "terminated":
            notes.append(f"{agent_id}: seats=finished")
            continue
        if status is None:
            notes.append(f"{agent_id}: seats=status-unknown")
            continue
        wanted = _seat_wake_wanted(
            missing,
            previous=None if previous is None else previous.absent,
            woke_at=None if previous is None else previous.woke_at,
            now=readings.now,
            cooldown_s=readings.cooldown_s,
        )
        next_states[agent_id] = SeatState(
            absent=missing,
            woke_at=readings.now if wanted else (previous.woke_at if previous else None),
        )
        if wanted:
            wakes.append(
                SeatWake(
                    agent_id=agent_id,
                    absent=missing,
                    text=_seat_wake_text(
                        agent_id,
                        missing,
                        registry_path=f"{readings.registry_dir}/{agent_id}.json",
                    ),
                )
            )
    return tuple(wakes), next_states, tuple(notes)
