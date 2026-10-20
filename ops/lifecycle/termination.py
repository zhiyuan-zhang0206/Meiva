# meiva:note
# Durable termination messages and the force termination transaction.

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.incarnation.lifecycle_commands import (
    KILL_ALL_SHELL_SESSIONS,
    record_unowned_termination,
)
from base.agents.messages.envelope import validate_source
from base.api_contracts.agent_contract import AgentNotFound, AgentStatus
from base.db import Database
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus
from base.log import logger
from base.telemetry.audit_events import prepare_event_log, record_audit
from ops.lifecycle.events import publish_page_closed as publish_page_closed
from ops.pages import list_open_page_names


# meiva:note
# Insert an optional pending chat followed by its terminate command.
#
#     `kill_all_shell_sessions` rides the command's payload
#     (`base.agents.incarnation.lifecycle_commands.KILL_ALL_SHELL_SESSIONS`), so the request is
#     durable in the same statement as the termination it accompanies.
def insert_termination_pair(
    conn: psycopg.Connection,
    agent_id: int,
    *,
    source: str,
    message: str | None,
    kill_all_shell_sessions: bool = False,
) -> tuple[int | None, int]:
    message_id: int | None = None
    with conn.cursor() as cur:
        if message is not None:
            validate_source(source)
            cur.execute(
                "INSERT INTO inbound_messages (agent_id,content,kind,source) "
                "VALUES (%s,%s,'chat',%s) RETURNING id",
                (agent_id, message, source),
            )
            message_row = cur.fetchone()
            if message_row is None:
                raise RuntimeError("termination message INSERT returned no id")
            message_id = int(message_row[0])
        cur.execute(
            "INSERT INTO inbound_messages (agent_id,content,kind,source,payload) "
            "VALUES (%s,'','terminate',%s,%s) RETURNING id",
            (
                agent_id,
                source,
                Jsonb({KILL_ALL_SHELL_SESSIONS: True}) if kill_all_shell_sessions else None,
            ),
        )
        terminate_row = cur.fetchone()
        if terminate_row is None:
            raise RuntimeError("terminate inbound INSERT returned no id")
    return message_id, int(terminate_row[0])


def insert_pending_termination_message(
    conn: psycopg.Connection,
    agent_id: int,
    *,
    source: str,
    message: str,
) -> int:
    """Retry only the final chat after the termination command is durable."""
    validate_source(source)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id,content,kind,source) "
            "VALUES (%s,%s,'chat',%s) RETURNING id",
            (agent_id, message, source),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("termination message retry INSERT returned no id")
        return int(row[0])


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


def _stage_termination_event(
    conn: psycopg.Connection,
    *,
    agent_id: int,
    source: str,
    inbound_id: int,
    kill_all_shell_sessions: bool,
) -> telemetry.Event:
    """Register a terminate audit fact before its operation commits.

    A requested shell-session kill is named in the payload next to the command
    it accompanies.
    """
    payload: dict[str, object] = {"inbound_id": inbound_id}
    if kill_all_shell_sessions:
        payload[KILL_ALL_SHELL_SESSIONS] = True
    event = prepare_event_log(
        event_type="terminate", agent_id=agent_id, source=source, payload=payload
    )
    from base.agents.impersonation.manifest import record_central_event

    return record_audit(conn, record_central_event(conn, event))


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
    """Persist graceful termination and publish its audit/wake effects.

    With `kill_all_shell_sessions` the agent row is locked first and the
    request rides the terminate command, so it serializes against the home
    runtime's apply (which locks the same row): either the apply sees the
    request and attempts older sessions after the termination commits, or this
    transaction sees the row already terminated and returns its shell cutoff
    with no command ID. The caller then attempts those older sessions instead
    of queueing a request no apply will ever read. Without the option the
    persistence path is unchanged and the returned cutoff is None.
    """
    with write_transaction(db_pool) as conn:
        if kill_all_shell_sessions:
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
#     asks the home host to cancel the running turn. `kill_all_shell_sessions` is
#     recorded on the force command and in its audit event; the returned shell
#     cutoff is captured under this row lock, and after commit the caller attempts
#     older sessions. A force kills sessions only when asked itself, so any open
#     graceful terminate, with its own shell-session request, is closed unserved.
#
#     `last_force_terminate_inbound_id` fences automatic resurrection: input that
#     was queued before the latest explicit force cannot revive the agent, even
#     when the force found it already terminated and changed no status.
def force_terminate_transaction(
    agent_id: int,
    db_pool: ConnectionPool,
    *,
    source: str,
    message: str | None = None,
    kill_all_shell_sessions: bool = False,
) -> tuple[AgentStatus, int | None, list[str], int, int]:
    with db_pool.connection() as conn, conn.cursor() as cur:
        conn.execute("SET TRANSACTION READ WRITE")
        cur.execute(
            "SELECT status, pid,session_index FROM agents_meta WHERE id = %s FOR UPDATE",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        old_status = AgentStatus(row[0])
        pid = row[1]
        page_names = list_open_page_names(conn, agent_id)
        _, terminate_inbound_id = _insert_termination_inbounds(
            conn,
            agent_id,
            source=source,
            message=message,
            kill_all_shell_sessions=kill_all_shell_sessions,
        )
        # Judged on the row as it is before this force ends it: no incarnation
        # to settle leaves a receipt resurrection accepts.
        record_unowned_termination(conn, agent_id, terminate_inbound_id)
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
            # termination_source='user': force-kill / a terminate that found the
            # pid already dead. Both are the user's will to end the agent, so it
            # is NOT crash-auto-resurrect-eligible even with a queued inbound.
            "UPDATE agents_meta SET status='terminated', termination_source='user', "
            "heartbeat_paused_until = NULL, last_force_terminate_inbound_id = %s "
            "WHERE id = %s",
            (terminate_inbound_id, agent_id),
        )
        prepared_event = _stage_termination_event(
            conn,
            agent_id=agent_id,
            source=source,
            inbound_id=terminate_inbound_id,
            kill_all_shell_sessions=kill_all_shell_sessions,
        )
    telemetry.emit_prepared(prepared_event)
    return old_status, pid, page_names, terminate_inbound_id, row[2]


# meiva:note
# Publish the non-transactional wake after the audit commit.
def publish_force_terminate_inbound(
    db: Database, bus: EventBus, agent_id: int, inbound_id: int, _source: str
) -> None:
    publish_inbound_wake(db, bus, agent_id, str(inbound_id))


# meiva:note
# Terminate by force and return the affected page names.
def force_mark_terminated(
    db: Database,
    bus: EventBus,
    agent_id: int,
    db_pool: ConnectionPool,
    *,
    source: str = "user",
    message: str | None = None,
) -> list[str]:
    _, _, page_names, inbound_id, _cutoff = force_terminate_transaction(
        agent_id,
        db_pool,
        source=source,
        message=message,
    )
    publish_force_terminate_inbound(db, bus, agent_id, inbound_id, source)
    return page_names
