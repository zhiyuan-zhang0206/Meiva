"""Resume a terminated agent by preserving its identity and enqueuing a wake."""

from datetime import datetime
from typing import LiteralString

import psycopg
from psycopg import sql

from base import telemetry
from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.incarnation.lifecycle_commands import supersede_lifecycle_for_resurrect
from base.agents.messages.inbound import WakeTriggerKind, validate_wake_trigger_kind
from base.api_contracts.agent_contract import (
    AgentNotFound,
    AgentStatus,
    MachinePaused,
    ResurrectAlreadyAlive,
)
from base.db import Database
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from base.log import logger
from base.telemetry.audit_events import prepare_event_log, record_audit
from ops.agents.resurrection_retry import ResurrectTriggerStaleError as ResurrectTriggerStaleError
from ops.agents.resurrection_retry import lock_active_home_machine as _lock_active_home_machine

# meiva:note
# The locked row the gate judges.
RESURRECTION_ROW: LiteralString = (
    "SELECT status,machine,permanent_reject_streak,last_permanent_reject_reason "
    "FROM agents_meta WHERE id = %s FOR UPDATE"
)


# meiva:note
# Run the one final resurrection CAS with a fully static SQL shape.
#
# The row leaves unowned: the next admission mints a fresh runtime generation.
# A trigger inbound must be pending, newer than the termination and newer than
# every terminate command of the agent, so work queued before a kill (a force
# on an already terminated agent changes no status) cannot reverse it.
def transition_terminated_to_unclaimed_idling(
    cur: psycopg.Cursor,
    agent_id: int,
    *,
    trigger_inbound_id: int | None,
    trigger_inbound_kind: WakeTriggerKind | None,
) -> datetime:
    if trigger_inbound_id is not None:
        from base.agents.recovery.breaker import RECOVERY_BREAKER_CLEAR

        assert trigger_inbound_kind is not None  # validated at public helper boundary  # noqa: S101
        cur.execute(
            sql.SQL(
                "UPDATE agents_meta SET status = %s, termination_source = NULL, "
                "last_turn_fatal_at = NULL, runtime_generation = NULL, runtime_owner = NULL "
                "WHERE id = %s AND status = %s "
                "AND {} "
                "AND EXISTS ("
                "  SELECT 1 FROM inbound_messages m "
                "  WHERE m.id = %s AND m.agent_id = agents_meta.id "
                "    AND m.status = 'pending' AND m.kind = %s "
                "    AND m.created_at > agents_meta.status_changed_at "
                "    AND m.id > COALESCE((SELECT max(t.id) FROM inbound_messages t "
                "        WHERE t.agent_id = agents_meta.id AND t.kind = 'terminate'), 0)"
                ") RETURNING status_changed_at"
            ).format(sql.SQL("TRUE" if trigger_inbound_kind == "chat" else RECOVERY_BREAKER_CLEAR)),
            (
                AgentStatus.IDLING,
                agent_id,
                AgentStatus.TERMINATED,
                trigger_inbound_id,
                trigger_inbound_kind,
            ),
        )
    else:
        cur.execute(
            "UPDATE agents_meta SET status = %s, termination_source = NULL, "
            "last_turn_fatal_at = NULL, runtime_generation = NULL, runtime_owner = NULL "
            "WHERE id = %s AND status = %s RETURNING status_changed_at",
            (AgentStatus.IDLING, agent_id, AgentStatus.TERMINATED),
        )
    transition_row = cur.fetchone()
    if transition_row is not None:
        if trigger_inbound_kind == "chat":
            cur.execute(
                "UPDATE agents_meta SET permanent_reject_streak=0, "
                "last_permanent_reject_reason=NULL, wake_suppressed_until=NULL, "
                "wake_suppress_reason=NULL WHERE id=%s",
                (agent_id,),
            )
        return transition_row[0]
    cur.execute(
        "SELECT home.paused_at IS NOT NULL "
        "FROM agents_meta a JOIN machines home ON home.name = a.machine "
        "WHERE a.id = %s",
        (agent_id,),
    )
    paused_row = cur.fetchone()
    if paused_row is not None and paused_row[0] is True:
        raise MachinePaused(
            f"agent {agent_id} home machine is paused; resume it before resurrecting"
        )
    if trigger_inbound_id is not None:
        raise ResurrectTriggerStaleError(
            f"agent {agent_id} trigger work no longer qualifies for its current "
            "termination; UPDATE affected 0 rows (stale work, suppressed automatic "
            "wakes, or a tripped recovery breaker)"
        )
    raise ResurrectAlreadyAlive(
        f"agent {agent_id} was concurrently modified after SELECT; UPDATE affected 0 rows"
    )


def _resurrect_event_target(resurrected_by: str) -> int | None:
    if not resurrected_by.startswith("agent:"):
        return None
    try:
        return int(resurrected_by.removeprefix("agent:"))
    except ValueError:
        return None


def _prepare_resurrect_attempt(
    db: Database,
    bus: EventBus,
    agent_id: int,
    *,
    resurrected_by: str,
    prompt: str | None,
    trigger_inbound_id: int | None,
    trigger_inbound_kind: WakeTriggerKind | None,
) -> telemetry.Event:
    """Commit ordinary resurrection and its optional prompt before waking the host."""
    with db.write_transaction() as conn, conn.cursor() as cur:
        # Task before agent: producers hold the task while inserting an inbound
        # whose foreign key can lock agents_meta. Keep the same lock order.
        from base.agents.tasks.delivery import lock_task_note_owner

        if trigger_inbound_id is not None and not lock_task_note_owner(
            cur, agent_id, trigger_inbound_id
        ):
            raise ResurrectTriggerStaleError(
                f"agent {agent_id} task assignment is no longer current"
            )
        latched_machine = _lock_active_home_machine(cur, agent_id)
        cur.execute(RESURRECTION_ROW, (agent_id,))
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        if row[1] != latched_machine:
            raise ResurrectTriggerStaleError("resurrection placement changed after pause latch")
        current = AgentStatus(row[0])
        if current is not AgentStatus.TERMINATED:
            raise ResurrectAlreadyAlive(
                f"agent {agent_id} is in {current.value!r} state, not 'terminated'"
            )
        # meiva:note
        # The resurrection inbound is inserted first so its id can close every
        # earlier open lifecycle command as superseded right here (issue
        # #2158): a restart or terminate queued in the previous life must not
        # act on the new one. A refusal below rolls the whole transaction back.
        # Stamp after the lock: a transaction begun before termination committed
        # must not sort its resurrection ahead of the interruption notices.
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, created_at, payload) "
            "VALUES (%s, '', 'resurrect', %s, clock_timestamp(), NULL) RETURNING id",
            (agent_id, resurrected_by),
        )
        resurrect_row = cur.fetchone()
        if resurrect_row is None:
            raise RuntimeError("resurrect lifecycle inbound INSERT returned no id")
        supersede_lifecycle_for_resurrect(conn, agent_id, resurrect_row[0])
        transition_terminated_to_unclaimed_idling(
            cur,
            agent_id,
            trigger_inbound_id=trigger_inbound_id,
            trigger_inbound_kind=trigger_inbound_kind,
        )
        if prompt is not None:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, created_at) "
                "VALUES (%s, %s, 'chat', %s, clock_timestamp())",
                (agent_id, prompt, resurrected_by),
            )
        prepared_event = _stage_resurrect_event(
            conn,
            agent_id,
            resurrected_by,
            prompt,
        )
        conn.commit()
        publish_agent_updated_sync(bus, agent_id)
    publish_inbound_wake(db, bus, agent_id, "0")
    return prepared_event


def _stage_resurrect_event(
    conn: psycopg.Connection,
    agent_id: int,
    resurrected_by: str,
    prompt: str | None,
) -> telemetry.Event:
    """Stage the exact resurrection audit fact inside the owning transaction."""
    payload: dict[str, object] = {"prompt": prompt} if prompt else {}
    event = prepare_event_log(
        event_type="resurrect",
        agent_id=agent_id,
        source=resurrected_by,
        target_agent_id=_resurrect_event_target(resurrected_by),
        payload=payload,
    )
    from base.agents.impersonation.manifest import record_central_event

    return record_audit(conn, record_central_event(conn, event))


# meiva:note
# Resume a terminated hosted incarnation for explicit or exact new input.
#
# The home machine must be unpaused. Pending triggers must be newer than
# termination and than every terminate command. A permanent provider
# error blocks background triggers; new chat clears that halt as an explicit
# attempt after the fix. Resurrection does not replay prior graph work.
def resurrect_agent(
    db: Database,
    bus: EventBus,
    agent_id: int,
    *,
    resurrected_by: str,
    prompt: str | None = None,
    trigger_inbound_id: int | None = None,
    trigger_inbound_kind: WakeTriggerKind | None = None,
) -> int:
    if (trigger_inbound_id is None) != (trigger_inbound_kind is None):
        raise ValueError("trigger inbound id and kind must be provided together")
    if trigger_inbound_kind is not None:
        trigger_inbound_kind = validate_wake_trigger_kind(trigger_inbound_kind)
    prepared_event = _prepare_resurrect_attempt(
        db,
        bus,
        agent_id,
        resurrected_by=resurrected_by,
        prompt=prompt,
        trigger_inbound_id=trigger_inbound_id,
        trigger_inbound_kind=trigger_inbound_kind,
    )
    telemetry.emit_prepared(prepared_event)
    logger.info(
        "agent {agent_id} resurrected by {resurrected_by}",
        event="agent_resurrected",
        agent_id=agent_id,
        resurrected_by=resurrected_by,
    )
    return agent_id
