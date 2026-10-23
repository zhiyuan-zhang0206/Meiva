# meiva:note
# Which pending inbounds count as an interrupt: the durable DB signal that `subscribe_interrupt`
# turns into an event. A cancel or terminate counts at once, a restart once its grace has passed,
# the agent's own terminate never does, and no read claims or consumes the row; claim still owns
# the command.
# meiva:link agent/graph/docs/turn-interrupts.meiva.okf.md

import asyncio

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.db import has_pending_interrupt, pending_interrupt_reason
from agent.graph.interrupt import subscribe_interrupt
from base.agents.messages.inbound import InterruptReason
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import create_agent
from base.native_process.stop_timing import RESTART_INTERRUPT_GRACE_S
from base.native_process.turn_identity import HostedTurnResources
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources

_TIMEOUT_S = 5.0

# The payload a maintenance drain stamps on its restart command.
_MAINTENANCE_PAYLOAD: dict[str, object] = {
    "maintenance": {"holder": "ops:test:interrupt", "acquired_at": "2026-09-19T00:00:00+00:00"}
}


# meiva:note
# Queue a command that has already waited `waited_s` (a restart's grace runs from its row).
def _insert(
    conn: psycopg.Connection,
    agent_id: int,
    kind: str,
    source: str = "user",
    payload: dict[str, object] | None = None,
    waited_s: float = 0.0,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload, created_at) "
            "VALUES (%s, '', %s, %s, %s, clock_timestamp() - make_interval(secs => %s))",
            (agent_id, kind, source, Jsonb(payload) if payload is not None else None, waited_s),
        )
    conn.commit()


class TestHasPendingInterrupt:
    @pytest.mark.parametrize(
        ("source", "reason"),
        [("user", InterruptReason.USER), ("agent:9", InterruptReason.SYSTEM)],
    )
    async def test_reason_retains_first_pending_command(
        self,
        hosted_resources: HostedTurnResources,
        db_conn,
        aops_pool: AsyncConnectionPool,
        source: str,
        reason: InterruptReason,
    ):
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "cancel", source=source)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "cancel", source="user")  # pyright: ignore[reportUnknownArgumentType]
        assert await pending_interrupt_reason(aops_pool, tid) is reason
        async with subscribe_interrupt(aops_pool, tid, resources=hosted_resources) as event:
            await asyncio.wait_for(event.wait(), timeout=_TIMEOUT_S)
            assert event.reason is reason
            event.set(
                InterruptReason.SYSTEM if reason is InterruptReason.USER else InterruptReason.USER
            )
            assert event.reason is reason
        assert db_conn.execute(  # pyright: ignore[reportUnknownMemberType]
            "SELECT status,claimed_at FROM inbound_messages WHERE agent_id=%s ORDER BY id",
            (tid,),
        ).fetchall() == [("pending", None), ("pending", None)]

    async def test_false_when_empty(self, db_conn, aops_pool: AsyncConnectionPool):
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        assert await has_pending_interrupt(aops_pool, tid) is False

    async def test_true_on_cancel(self, db_conn, aops_pool: AsyncConnectionPool):
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "cancel")  # pyright: ignore[reportUnknownArgumentType]
        assert await has_pending_interrupt(aops_pool, tid) is True

    async def test_true_on_terminate(self, db_conn, aops_pool: AsyncConnectionPool):
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "terminate")  # pyright: ignore[reportUnknownArgumentType]
        assert await has_pending_interrupt(aops_pool, tid) is True

    async def test_false_on_chat_only(self, db_conn, aops_pool: AsyncConnectionPool):
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "chat")  # pyright: ignore[reportUnknownArgumentType]
        assert await has_pending_interrupt(aops_pool, tid) is False

    async def test_ignores_other_agent(self, db_conn, aops_pool: AsyncConnectionPool):
        mine = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        other = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, other, "cancel")  # pyright: ignore[reportUnknownArgumentType]
        assert await has_pending_interrupt(aops_pool, mine) is False

    async def test_ignores_self_initiated_terminate(self, db_conn, aops_pool: AsyncConnectionPool):
        # meiva.self.terminate() inserts a terminate row source='self' then raises
        # AgentTermination in-thread; the in-flight watcher must NOT fire on it
        # (it would inject a KeyboardInterrupt into the self-terminating thread,
        # racing the clean lifecycle exit). claim still dispatches the row.
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "terminate", source="self")  # pyright: ignore[reportUnknownArgumentType]
        assert await has_pending_interrupt(aops_pool, tid) is False

    async def test_external_terminate_still_fires(self, db_conn, aops_pool: AsyncConnectionPool):
        # a peer / admin / user terminate (non-self source) does interrupt mid-turn
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(db_conn, tid, "terminate", source="agent:9")  # pyright: ignore[reportUnknownArgumentType]
        assert await has_pending_interrupt(aops_pool, tid) is True

    async def test_false_on_unmarked_maintenance_restart(
        self, db_conn, aops_pool: AsyncConnectionPool, *, machine_identity: MachineIdentity
    ):
        # meiva:note
        # The drain waits for the turn boundary: a restart younger than its grace never
        # aborts in-flight work.
        tid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        _insert(
            db_conn,  # pyright: ignore[reportUnknownArgumentType]
            tid,
            "restart",
            source="system:restart",
            payload=_MAINTENANCE_PAYLOAD,
        )
        db_conn.execute(  # pyright: ignore[reportUnknownMemberType]
            "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'running',%s)",
            (tid, machine_identity.name()),
        )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        assert await has_pending_interrupt(aops_pool, tid) is False


def _age_restarts(conn: psycopg.Connection, agent_id: int, seconds: float) -> None:
    conn.execute(
        "UPDATE inbound_messages SET created_at = clock_timestamp() - make_interval(secs => %s) "
        "WHERE agent_id=%s AND kind='restart'",
        (seconds, agent_id),
    )
    conn.commit()


# meiva:note
# A restart past its grace sets the watcher's event as a restart; claim still owns the row.
async def test_the_watcher_fires_with_the_restart_reason_and_leaves_the_command_pending(
    hosted_resources: HostedTurnResources,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    tid = create_agent(db_conn)
    _insert(db_conn, tid, "restart")
    async with subscribe_interrupt(
        aops_pool, tid, resources=hosted_resources, poll_s=0.05
    ) as event:
        await asyncio.sleep(0.2)
        assert not event.is_set()
        _age_restarts(db_conn, tid, RESTART_INTERRUPT_GRACE_S + 2)
        await asyncio.wait_for(event.wait(), timeout=_TIMEOUT_S)
        assert event.reason is InterruptReason.RESTART
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s", (tid,)
    ).fetchall() == [("pending",)]
