# meiva:note
# A restart is an interrupt once it has waited its grace. `pending_interrupt_reason` reads the
# grace from the restart row's own `created_at`, so these tests age the row instead of waiting.

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.db import has_pending_interrupt, pending_interrupt_reason
from base.agents.messages.inbound import InterruptReason
from base.db import create_agent
from base.native_process.stop_timing import RESTART_INTERRUPT_GRACE_S


def _queue(
    conn: psycopg.Connection, agent_id: int, kind: str, *, source: str = "user", waited_s: float = 0
) -> None:
    conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, created_at) "
        "VALUES (%s, '', %s, %s, clock_timestamp() - make_interval(secs => %s))",
        (agent_id, kind, source, waited_s),
    )
    conn.commit()


async def test_a_young_restart_does_not_interrupt(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = create_agent(db_conn)
    _queue(db_conn, agent_id, "restart", waited_s=RESTART_INTERRUPT_GRACE_S - 2)
    assert await pending_interrupt_reason(aops_pool, agent_id) is None


async def test_a_restart_that_has_waited_its_grace_interrupts_as_a_restart(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = create_agent(db_conn)
    _queue(db_conn, agent_id, "restart", waited_s=RESTART_INTERRUPT_GRACE_S + 2)
    assert await pending_interrupt_reason(aops_pool, agent_id) is InterruptReason.RESTART
    assert await has_pending_interrupt(aops_pool, agent_id) is True


async def test_a_restart_from_the_agent_itself_never_interrupts(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = create_agent(db_conn)
    _queue(db_conn, agent_id, "restart", source="self", waited_s=RESTART_INTERRUPT_GRACE_S * 10)
    assert await pending_interrupt_reason(aops_pool, agent_id) is None


async def test_a_claimed_restart_no_longer_interrupts(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = create_agent(db_conn)
    _queue(db_conn, agent_id, "restart", waited_s=RESTART_INTERRUPT_GRACE_S + 2)
    db_conn.execute("UPDATE inbound_messages SET status='claimed' WHERE agent_id=%s", (agent_id,))
    db_conn.commit()
    assert await pending_interrupt_reason(aops_pool, agent_id) is None


@pytest.mark.parametrize("kind", ["cancel", "terminate"])
async def test_cancel_and_terminate_still_interrupt_at_once_beside_a_young_restart(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, kind: str
) -> None:
    agent_id = create_agent(db_conn)
    _queue(db_conn, agent_id, "restart")
    _queue(db_conn, agent_id, kind)
    assert await pending_interrupt_reason(aops_pool, agent_id) is InterruptReason.USER
