# meiva:note
# The effects of the agent watch: read each watched duty's signals from the database, ask the rule
# in `schedules/plans/agent_watch_rule.py`, carry the states in one JSON file under the running
# host's MEIVA_HOME, send the messages the rule asks for, and record the round as one telemetry
# event.
#
# A signal this host cannot read is not a healthy signal: `observe` reports it in `blind`, and the
# round's line and messages carry it. The watch itself is one round per fire and holds no loop --
# the cron trigger decides the cadence, so a round that dies takes down nothing but its own slot.
# meiva:link schedules/README.md

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import meiva
from psycopg import Connection

from base.daemon.schedules.inputs import ScheduleInputs
from schedules.jobs.telemetry_event import emit_system_event
from schedules.plans.agent_watch_rule import (
    Observation,
    RoundPlan,
    WatchedAgent,
    WatchState,
    judge_schedule,
    plan_round,
    watched_agents,
)

__all__ = [
    "EVENT_NAME",
    "EmitTally",
    "observe",
    "read_state",
    "round_line",
    "run_round",
    "write_state",
]

EVENT_NAME = "agent_watch_round"
_FIRE_WINDOW = 20


# meiva:note
# Emit failures since this process started. A declared name is the fix for the failure that
# shipped; a counter is the fix for it happening again quietly: the round's line carries the count,
# so a monitor whose telemetry is dead reads as a monitor with a number on its line rather than as
# a monitor with nothing to say. The composition root owns the tally and hands it in -- a module
# keeps no state of its own, and the schedule's process is the thing whose lifetime the count
# describes.
@dataclass
class EmitTally:
    failures: int = 0


_STATUS_AND_ACTIVE = (
    "SELECT status, extract(epoch FROM now() - last_active_at) / 60, "
    "CASE WHEN heartbeat_paused_until > now() "
    "THEN extract(epoch FROM heartbeat_paused_until - now()) / 60 END "
    "FROM agents_meta WHERE id = %s"
)
_LIVE_SESSIONS = "SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s AND expires_at > now()"
_SCHEDULE = "SELECT id, enabled FROM schedules WHERE name = %s"
_FIRES = (
    "SELECT slot_fire_at FROM schedule_fire_log WHERE schedule_id = %s "
    "ORDER BY slot_fire_at DESC LIMIT %s"
)


# meiva:note
# `late` and `blind` are separate on purpose: a schedule that is switched off owes nothing, so it
# appears in neither list.
def _schedule_signals(
    conn: Connection, entry: WatchedAgent, now: datetime
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    late: list[str] = []
    blind: list[str] = []
    for name in entry.schedules:
        with conn.cursor() as cur:
            cur.execute(_SCHEDULE, (name,))
            row = cur.fetchone()
            if row is None or not row[1]:
                continue
            cur.execute(_FIRES, (row[0], _FIRE_WINDOW))
            fires = tuple(item[0] for item in cur.fetchall())
        verdict = judge_schedule(name, fires, now=now)
        if verdict.late is not None:
            late.append(verdict.late)
        if verdict.blind is not None:
            blind.append(verdict.blind)
    return tuple(late), tuple(blind)


# meiva:note
# One round's readings for one duty. A terminated or absent agent answers with its status alone:
# the rule reads that as finished, so a duty the user ended is never resurrected by a sentinel. The
# declared heartbeat pause comes from the same row, so a duty that said it is resting is not read as
# one that stopped.
def observe(conn: Connection, entry: WatchedAgent, *, now: datetime) -> Observation:
    with conn.cursor() as cur:
        cur.execute(_STATUS_AND_ACTIVE, (entry.agent_id,))
        row = cur.fetchone()
        if row is None:
            return Observation("missing", 0, None, blind=("no agents_meta row",))
        status, active_minutes = row[0], (float(row[1]) if row[1] is not None else None)
        paused_minutes = float(row[2]) if row[2] is not None else None
        if status == "terminated":
            return Observation(status, 0, active_minutes)
        cur.execute(_LIVE_SESSIONS, (entry.agent_id,))
        counted = cur.fetchone()
        assert counted is not None
        sessions = int(counted[0])
    late, blind = _schedule_signals(conn, entry, now)
    unread = list(blind)
    if active_minutes is None:
        unread.append("active: NULL last_active_at")
    return Observation(status, sessions, active_minutes, paused_minutes, late, tuple(unread))


def read_state(path: Path) -> dict[int, WatchState]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    states: dict[int, WatchState] = {}
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        try:
            states[int(key)] = WatchState(
                silent_since=value.get("silent_since"),
                last_wake_at=float(value.get("last_wake_at") or 0.0),
                last_report_at=float(value.get("last_report_at") or 0.0),
                last_escalate_at=float(value.get("last_escalate_at") or 0.0),
            )
        except (TypeError, ValueError):
            continue
    return states


# meiva:note
# Written whole, to a temporary file first: a round that dies mid-write leaves the previous states
# rather than a half file the next round cannot parse.
def write_state(path: Path, states: dict[int, WatchState]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        {str(agent_id): asdict(state) for agent_id, state in sorted(states.items())},
        indent=2,
        sort_keys=True,
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def round_line(
    entries: Sequence[WatchedAgent],
    plans: Sequence[tuple[WatchedAgent, RoundPlan]],
    observations: dict[int, Observation],
    *,
    emit_failures: int = 0,
) -> str:
    silent = [entry.agent_id for entry, plan in plans if plan.reasons]
    blind = [agent_id for agent_id, item in observations.items() if item.blind]
    sent = sum(len(plan.actions) for _entry, plan in plans)
    parts = [
        f"round watched={len(entries)} silent={len(silent)} blind={len(blind)} sent={sent}",
    ]
    if silent:
        parts.append("silent_ids=" + ",".join(str(item) for item in silent))
    if blind:
        parts.append("blind_ids=" + ",".join(str(item) for item in blind))
    if emit_failures:
        parts.append(f"emit_failures={emit_failures}")
    return " ".join(parts)


# meiva:note
# One round: read, decide, send, save, record. A send that fails is a line in the round, not the
# end of it: one unreachable agent must not stop the round from checking the others, and the
# failure is visible in the same place the round's other readings are.
def run_round(
    *,
    inputs: ScheduleInputs,
    report_agent: int,
    state_path: Path,
    now: datetime,
    process: str,
    tally: EmitTally,
) -> str:
    entries = watched_agents()
    states = read_state(state_path)
    stamp = now.isoformat(timespec="seconds")
    with inputs.database().connect(autocommit=True) as conn:
        observations = {entry.agent_id: observe(conn, entry, now=now) for entry in entries}
    notes: list[str] = []
    plans: list[tuple[WatchedAgent, RoundPlan]] = []
    for entry in entries:
        plan = plan_round(
            entry,
            observations[entry.agent_id],
            states.get(entry.agent_id, WatchState()),
            now=now.timestamp(),
            stamp=stamp,
            report_agent=report_agent,
        )
        states[entry.agent_id] = plan.state
        plans.append((entry, plan))
        notes.extend(plan.log)
        for action in plan.actions:
            try:
                meiva.agents.send_message(action.target, action.text)
                notes.append(f"{entry.agent_id}: {action.kind} -> {action.target}")
            except Exception as exc:
                notes.append(f"{entry.agent_id}: {action.kind} failed: {type(exc).__name__}: {exc}")
    write_state(state_path, states)
    try:
        emit_system_event(
            EVENT_NAME,
            {
                "watched": len(entries),
                "silent": sum(1 for _entry, plan in plans if plan.reasons),
                "sent": sum(len(plan.actions) for _entry, plan in plans),
                "blind": sum(1 for item in observations.values() if item.blind),
            },
            process=process,
            producer=inputs.producer,
            machine_reader=inputs.machine.name,
            image=inputs.image,
            home=inputs.home,
        )
    except Exception as exc:
        tally.failures += 1
        notes.append(
            f"event emit failed ({tally.failures} since start): {type(exc).__name__}: {exc}"
        )
    line = round_line(entries, plans, observations, emit_failures=tally.failures)
    return " | ".join([line, *notes])
