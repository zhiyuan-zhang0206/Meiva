from __future__ import annotations

import asyncio

import psycopg

from agent.graph.llm_errors import HarnessQuotaExhaustedError
from base.agents.recovery.breaker import SUPPRESS_REASON_QUOTA_EXHAUSTED
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.packages.tests.model_catalog import model_catalog as model_catalog
from services.agent_runner.agent_host.tests.runtime.host_support import (
    Build,
    BuildWith,
    EventTap,
    Row,
    ScriptedGraph,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    event_tap as event_tap,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    host_plugin as host_plugin,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    host_pools as host_pools,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    rig as rig,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    wired as wired,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    wired_with as wired_with,
)


async def test_pending_inbound_wakes_skips_active_quota_backoff_until_expiry_or_control(
    wired: Build,
    db_conn: psycopg.Connection,
) -> None:
    host, _, _ = wired(
        {
            17: Row(status="idling", harness="antigravity"),
            23: Row(status="idling", harness="claude_code"),
        }
    )
    db_conn.execute(
        "UPDATE agents_meta SET wake_suppress_reason = %s, "
        "wake_suppressed_until = now() + interval '10 minutes' WHERE id IN (17, 23)",
        (SUPPRESS_REASON_QUOTA_EXHAUSTED,),
    )
    db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
        "VALUES (17, 'wait for reset', 'chat', 'user', 'pending')"
    )
    db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status, claimed_at) "
        "VALUES (23, 'orphaned claimed', 'chat', 'user', 'claimed', now())"
    )
    db_conn.commit()

    assert await host.pending_inbound_wakes() == []

    # meiva:note
    # Operator restart control bypasses quota backoff so lifecycle commands always run.
    ctrl_row = db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
        "VALUES (17, 'manual restart', 'restart', 'user', 'pending') RETURNING id"
    ).fetchone()
    assert ctrl_row is not None
    db_conn.commit()
    assert await host.pending_inbound_wakes() == [17]

    db_conn.execute("DELETE FROM inbound_messages WHERE id = %s", (ctrl_row[0],))
    db_conn.execute(
        "UPDATE agents_meta SET wake_suppressed_until = now() - interval '1 second' "
        "WHERE id IN (17, 23)"
    )
    db_conn.commit()
    assert sorted(await host.pending_inbound_wakes()) == [17, 23]


async def test_hosted_quota_exhausted_abort_idles_without_crash_corpse_or_clearing_backoff(
    wired_with: BuildWith,
    db_conn: psycopg.Connection,
    event_tap: EventTap,
) -> None:
    agent_id = 17
    quota_exc = HarnessQuotaExhaustedError(
        "antigravity harness quota exhausted: Individual quota reached. Resets in 10m.",
        retry_after_seconds=605.0,
        reset_source="parsed_reset",
        requeued_inbound_ids=(101,),
    )

    class _QuotaFailGraph(ScriptedGraph):
        async def ainvoke(self, *args: object, **kwargs: object) -> dict[str, object]:
            # meiva:note
            # Stamp agents_meta the way settle_quota_abort does right before raising.
            db_conn.execute(
                "UPDATE agents_meta SET wake_suppress_reason = %s, "
                "wake_suppressed_until = now() + interval '605 seconds' WHERE id = %s",
                (SUPPRESS_REASON_QUOTA_EXHAUSTED, agent_id),
            )
            db_conn.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
                "VALUES (%s, 'requeued user chat', 'chat', 'user', 'pending')",
                (agent_id,),
            )
            db_conn.commit()
            raise quota_exc

    host, _, _ = wired_with(
        {agent_id: Row(status="idling", harness="antigravity")},
        _QuotaFailGraph({}),
    )
    await asyncio.wait_for(host.run_turn(agent_id), 10)

    row = db_conn.execute(
        "SELECT status, last_turn_fatal_at, last_turn_fatal_reason, "
        "wake_suppress_reason, wake_suppressed_until > now() "
        "FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert row == ("idling", None, None, SUPPRESS_REASON_QUOTA_EXHAUSTED, True)
    assert await host.pending_inbound_wakes() == []

    errors = await event_tap.events("error")
    assert len(errors) == 1
    assert "quota exhausted" in errors[0]["content"]
