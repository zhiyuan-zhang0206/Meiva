# meiva:note
# Durable termination messages and the force termination transaction.

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents.inbox.wake import publish_inbound_wake
from base.agents.incarnation.lifecycle_commands import KILL_ALL_SHELL_SESSIONS
from base.agents.messages.envelope import validate_source
from base.api_contracts.agent_contract import AgentNotFound, AgentStatus
from base.db import Database
from base.db.rows import IdRow, query_one
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus
from base.lm.harness.registration import RegistrationStore
from base.log import logger
from base.paths import harness_registrations_dir
from base.telemetry.audit_events import prepare_event_log, record_audit
from ops.rpc_schemas.terminate import HarnessAttachment


# meiva:note
# Insert an optional pending chat followed by its terminate command. The optional
# `kill_all_shell_sessions` payload remains an audit compatibility field; every
# termination closes the agent's persistent shells.
def insert_termination_pair(
    conn: psycopg.Connection,
    agent_id: int,
    *,
    source: str,
    message: str | None,
    kill_all_shell_sessions: bool = False,
) -> tuple[int | None, int]:
    message_id: int | None = None
    if message is not None:
        validate_source(source)
        message_row = query_one(
            conn,
            IdRow,
            "INSERT INTO inbound_messages (agent_id,content,kind,source) "
            "VALUES (%s,%s,'chat',%s) RETURNING id",
            (agent_id, message, source),
        )
        if message_row is None:
            raise RuntimeError("termination message INSERT returned no id")
        message_id = message_row.id
    terminate_row = query_one(
        conn,
        IdRow,
        "INSERT INTO inbound_messages (agent_id,content,kind,source,payload) "
        "VALUES (%s,'','terminate',%s,%s) RETURNING id",
        (
            agent_id,
            source,
            Jsonb({KILL_ALL_SHELL_SESSIONS: True}) if kill_all_shell_sessions else None,
        ),
    )
    if terminate_row is None:
        raise RuntimeError("terminate inbound INSERT returned no id")
    return message_id, terminate_row.id


def insert_pending_termination_message(
    conn: psycopg.Connection,
    agent_id: int,
    *,
    source: str,
    message: str,
) -> int:
    """Retry only the final chat after the termination command is durable."""
    validate_source(source)
    row = query_one(
        conn,
        IdRow,
        "INSERT INTO inbound_messages (agent_id,content,kind,source) "
        "VALUES (%s,%s,'chat',%s) RETURNING id",
        (agent_id, message, source),
    )
    if row is None:
        raise RuntimeError("termination message retry INSERT returned no id")
    return row.id


def _insert_termination_inbounds(
    conn: psycopg.Connection,
    agent_id: int,
    *,
    source: str,
    message: str | None,
    kill_all_shell_sessions: bool = False,
) -> tuple[int | None, int]:
    """Insert termination inbounds, preserving terminate on message failure.

    The caller owns the outer transaction. Nested transactions are savepoints:
    the first keeps the chat and command atomic, while the second contains a
    failed best-effort chat retry without rolling back the durable command.
    """
    kill = kill_all_shell_sessions
    if message is None:
        return insert_termination_pair(
            conn, agent_id, source=source, message=None, kill_all_shell_sessions=kill
        )
    try:
        with conn.transaction():
            return insert_termination_pair(
                conn, agent_id, source=source, message=message, kill_all_shell_sessions=kill
            )
    except Exception as exc:
        logger.warning(
            "atomic termination message enqueue failed for agent {agent_id}; "
            "retrying the terminate command alone ({exc!r})",
            agent_id=agent_id,
            exc=exc,
        )
    _, terminate_id = insert_termination_pair(
        conn, agent_id, source=source, message=None, kill_all_shell_sessions=kill
    )
    message_id: int | None = None
    try:
        with conn.transaction():
            message_id = insert_pending_termination_message(
                conn,
                agent_id,
                source=source,
                message=message,
            )
    except Exception as exc:
        logger.warning(
            "termination message retry failed for agent {agent_id}; the terminate "
            "command remains durable ({exc!r})",
            agent_id=agent_id,
            exc=exc,
        )
    return message_id, terminate_id


# meiva:note
# Audit records retain the legacy request marker without making shell cleanup
# optional, so historical request facts remain interpretable.
def _stage_termination_event(
    conn: psycopg.Connection,
    *,
    agent_id: int,
    source: str,
    inbound_id: int,
    kill_all_shell_sessions: bool,
) -> telemetry.Event:
    payload: dict[str, object] = {"inbound_id": inbound_id}
    if kill_all_shell_sessions:
        payload[KILL_ALL_SHELL_SESSIONS] = True
    event = prepare_event_log(
        event_type="terminate", agent_id=agent_id, source=source, payload=payload
    )
    return record_audit(conn, event)


# meiva:note
# The graceful path owns the row lock that fences shell allocation from the
# eventual terminal transition and supplies its cleanup cutoff.
def enqueue_termination_inbounds(
    db: Database,
    bus: EventBus,
    agent_id: int,
    db_pool: ConnectionPool,
    *,
    source: str,
    message: str | None,
    kill_all_shell_sessions: bool = False,
) -> tuple[int | None, int | None]:
    with write_transaction(db_pool) as conn:
        row = conn.execute(
            "SELECT status,session_index FROM agents_meta WHERE id = %s FOR UPDATE", (agent_id,)
        ).fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        if AgentStatus(row[0]) is AgentStatus.TERMINATED:
            return None, row[1]
        _, terminate_id = _insert_termination_inbounds(
            conn,
            agent_id,
            source=source,
            message=message,
            kill_all_shell_sessions=kill_all_shell_sessions,
        )
        prepared_event = _stage_termination_event(
            conn,
            agent_id=agent_id,
            source=source,
            inbound_id=terminate_id,
            kill_all_shell_sessions=kill_all_shell_sessions,
        )
    telemetry.emit_prepared(prepared_event)
    publish_force_terminate_inbound(db, bus, agent_id, terminate_id, source)
    return terminate_id, None


# meiva:note
# Lock the agent, write its termination and close every open lifecycle command.
#
#     Force writes `terminated` itself, in this one transaction; the caller then
#     asks the home host to cancel the running turn. The returned shell cutoff is
#     captured under this row lock, and after commit the caller closes every older
#     shell and drops its TTL rows.
#
#     The force's own terminate command fences automatic resurrection: input that
#     was queued before the latest explicit force carries a smaller inbound id and
#     cannot revive the agent, even when the force found it already terminated and
#     changed no status.
def force_terminate_transaction(
    agent_id: int,
    db_pool: ConnectionPool,
    *,
    source: str,
    message: str | None = None,
    kill_all_shell_sessions: bool = False,
    require_quiescent: bool = False,
) -> tuple[AgentStatus, int, int]:
    with db_pool.connection() as conn, conn.cursor() as cur:
        conn.execute("SET TRANSACTION READ WRITE")
        cur.execute(
            "SELECT status, session_index FROM agents_meta WHERE id = %s FOR UPDATE",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        old_status = AgentStatus(row[0])
        if require_quiescent and old_status not in (AgentStatus.IDLING, AgentStatus.TERMINATED):
            raise ValueError(f"Scheduled restart refused: agent {agent_id} is {old_status.value}")

        _, terminate_inbound_id = _insert_termination_inbounds(
            conn,
            agent_id,
            source=source,
            message=message,
            kill_all_shell_sessions=kill_all_shell_sessions,
        )
        # meiva:note
        # Older commands are closed visibly, never silently dropped; the force's
        # own command is simply done, since this transaction executed it.
        cur.execute(
            "UPDATE inbound_messages SET status='done', "
            "payload=COALESCE(payload,'{}'::jsonb)||%s "
            "WHERE agent_id=%s AND id<%s AND kind IN ('restart','terminate') "
            "AND status IN ('pending','claimed')",
            (
                Jsonb({"lifecycle_result": {"outcome": "superseded", "reason": "force_terminate"}}),
                agent_id,
                terminate_inbound_id,
            ),
        )
        cur.execute(
            "UPDATE inbound_messages SET status='done' WHERE id=%s", (terminate_inbound_id,)
        )
        cur.execute(
            # meiva:note
            # termination_source='user': a force-kill is the user's will to end the
            # agent, so it is NOT crash-auto-resurrect-eligible even with a queued
            # inbound.
            "UPDATE agents_meta SET status='terminated', termination_source='user', "
            "heartbeat_paused_until = NULL WHERE id = %s",
            (agent_id,),
        )
        prepared_event = _stage_termination_event(
            conn,
            agent_id=agent_id,
            source=source,
            inbound_id=terminate_inbound_id,
            kill_all_shell_sessions=kill_all_shell_sessions,
        )
    telemetry.emit_prepared(prepared_event)
    return old_status, terminate_inbound_id, row[1]


# meiva:note
# Publish the non-transactional wake after the audit commit.
def publish_force_terminate_inbound(
    db: Database, bus: EventBus, agent_id: int, inbound_id: int, _source: str
) -> None:
    publish_inbound_wake(db, bus, agent_id, str(inbound_id))


# meiva:note
# Terminate by force.
def force_mark_terminated(
    db: Database,
    bus: EventBus,
    agent_id: int,
    db_pool: ConnectionPool,
    *,
    source: str = "user",
    message: str | None = None,
) -> None:
    _, inbound_id, _cutoff = force_terminate_transaction(
        agent_id,
        db_pool,
        source=source,
        message=message,
    )
    publish_force_terminate_inbound(db, bus, agent_id, inbound_id, source)


# meiva:note
# A local lifecycle cleanup deletes only rows in the cutoff it conclusively
# closed; newer or concurrently rewritten rows remain for their owner/reaper.
def drop_shell_ttls_before_cutoff(db_pool: ConnectionPool, agent_id: int, cutoff: int) -> None:
    with write_transaction(db_pool) as conn:
        conn.execute(
            "DELETE FROM agent_shell_ttls WHERE agent_id = %s AND session_id < %s",
            (agent_id, cutoff),
        )


# meiva:note
# The registration row `home` holds for `agent_id`, as a terminate response reports it.
#
# The row is the bridge's record that a provider session is acting as this agent
# (`base.lm.harness.registration`), so reading it here is the one place a terminate request
# learns that somebody is still driving the agent. Absence is `None` and never an error, and a
# row that cannot be decoded is `None` with the reason logged: the hint is advisory, so a
# corrupt file must not turn a termination into a failure.
#
# Nothing here filters on expiry. A row whose claim has lapsed is still on disk until the
# bridge retires it or `sweep` drops it, and reporting it with its own past `expires_at` is what
# lets the caller tell "nobody attached" from "a session's claim lapsed".
#
# Nothing refuses on a present row. An attached agent has to stay killable — a controller that
# has wedged must not be able to keep its agent alive by staying attached — so the fact travels
# to the caller and the operator decides what to do with it.
def harness_attachment(agent_id: int, *, home: Path) -> HarnessAttachment | None:
    store = RegistrationStore(harness_registrations_dir(home=home))
    try:
        registration = store.read(agent_id)
    except (OSError, ValueError, TypeError) as exc:
        logger.error(
            "[lifecycle] harness registration of agent {} could not be read: {!r}", agent_id, exc
        )
        return None
    if registration is None:
        return None
    return HarnessAttachment(
        agent_id=registration.agent_id,
        controller_pid=registration.controller_pid,
        expires_at=registration.expires_at,
    )
