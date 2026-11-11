# meiva:note
# A restart is an interrupt once it has waited its grace. `pending_interrupt_reason` reads the
# grace from the restart row's own `created_at`, so these tests age the row instead of waiting.

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.db import has_pending_interrupt, pending_interrupt_reason
from base.agents.messages.inbound import InterruptReason, InterruptScope
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


@pytest.mark.parametrize("kind", ["interrupt", "terminate"])
async def test_cancel_and_terminate_still_interrupt_at_once_beside_a_young_restart(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, kind: str
) -> None:
    agent_id = create_agent(db_conn)
    _queue(db_conn, agent_id, "restart")
    _queue(db_conn, agent_id, kind)
    assert await pending_interrupt_reason(aops_pool, agent_id) is InterruptReason.USER


# meiva:note
# An external harness agent's own `meiva.self.terminate()` runs in a shell of the CLI, where no exec
# node raises `LifecycleExit`: its foreground (`InterruptScope.LIFECYCLE`) reads the row once it has
# waited the grace, and nothing else does.
async def test_an_own_terminate_interrupts_the_harness_foreground_after_its_grace(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = create_agent(db_conn)
    _queue(db_conn, agent_id, "terminate", source="self", waited_s=RESTART_INTERRUPT_GRACE_S + 2)
    assert (
        await pending_interrupt_reason(aops_pool, agent_id, scope=InterruptScope.LIFECYCLE)
        is InterruptReason.SYSTEM
    )


async def test_a_young_own_terminate_lets_the_turn_write_its_closing_words(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = create_agent(db_conn)
    _queue(db_conn, agent_id, "terminate", source="self", waited_s=RESTART_INTERRUPT_GRACE_S - 2)
    assert (
        await pending_interrupt_reason(aops_pool, agent_id, scope=InterruptScope.LIFECYCLE) is None
    )


@pytest.mark.parametrize("scope", [InterruptScope.ALL, InterruptScope.FOREGROUND])
async def test_a_native_node_and_the_cancel_watch_never_read_an_own_terminate(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, scope: InterruptScope
) -> None:
    agent_id = create_agent(db_conn)
    _queue(db_conn, agent_id, "terminate", source="self", waited_s=RESTART_INTERRUPT_GRACE_S * 10)
    assert await pending_interrupt_reason(aops_pool, agent_id, scope=scope) is None


async def test_an_own_restart_never_interrupts_the_harness_foreground(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = create_agent(db_conn)
    _queue(db_conn, agent_id, "restart", source="self", waited_s=RESTART_INTERRUPT_GRACE_S * 10)
    assert (
        await pending_interrupt_reason(aops_pool, agent_id, scope=InterruptScope.LIFECYCLE) is None
    )
