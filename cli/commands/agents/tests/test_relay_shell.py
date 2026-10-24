# meiva:note
# The relay shell against real resources: a real lease and inbox in the throwaway database, the
# real Redis wake listener, and the hosts it delivers into as listeners on this machine. Every
# decision the shell takes is covered as values in `relay/`; these cases prove the shell takes
# the steps the decisions return, in order, and that the lease's durable state tells the truth
# afterwards.

from __future__ import annotations

import asyncio

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.impersonation import relay as lease_relay
from base.agents.impersonation.host_transport import AntigravityServer
from base.agents.impersonation.status import ImpersonationStatus
from base.agents.inbox.insert import insert_inbound_message
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.events.live.bus import EventBus
from cli.commands.agents.impersonation_adapters import AntigravitySend, ClaudeStdout, CodexSteer
from cli.commands.agents.relay.steps import RelayTuning
from cli.commands.agents.tests.lease_support import (
    START_MESSAGE,
    ActiveSession,
    LeaseAt,
    assert_message_pending,
    delivery_attempts,
)
from cli.commands.agents.tests.lease_support import active as active
from cli.commands.agents.tests.lease_support import lease_at as lease_at
from cli.commands.agents.tests.loopback_hosts import (
    antigravity_server,
    codex_app_server,
    short_socket_path,
)
from cli.commands.agents.tests.relay_harness import (
    CSRF,
    FAST,
    SLOW,
    THREAD,
    Printed,
    relay_lease,
    running,
    until,
)


async def test_a_claude_relay_pushes_the_briefing_then_the_batch_and_stops_at_release(
    active: ActiveSession,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, mid = active
    printed = Printed()
    async with running(
        relay_lease(lease, machine_identity),
        database=database,
        event_bus=event_bus,
        redis_url=redis_url,
        adapter=ClaudeStdout(printed.stream),
    ) as task:
        await until(lambda: printed.contains("Unprocessed user input"), "the pushed batch")
        leases.release(
            database,
            event_bus,
            lease["id"],
            "Returning control",
            local_machine=machine_identity.name(),
        )
        await asyncio.wait_for(task, timeout=SLOW)

    out = printed.text
    briefing, _, batch = out.partition("Meiva message agent=")
    assert briefing.startswith(START_MESSAGE)
    assert "Each message has 2 delivery attempts, with 180s to ACK each" in briefing
    assert f"agent={lease['agent_id']} lease={lease['session_id']} ids={mid}" in out
    assert f"[id={mid}] kind=chat from=user" in batch
    assert (
        f"ACK after receiving this message: meiva impersonate ack {lease['session_id']} {mid} "
        f"--agent {lease['agent_id']}"
    ) in batch
    assert "ended:" not in out
    assert delivery_attempts(db_conn, mid) == 1
    assert_message_pending(db_conn, mid)


async def test_active_expiry_notifies_loss_of_control(
    active: ActiveSession,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, _ = active
    printed = Printed()
    async with running(
        relay_lease(lease, machine_identity),
        database=database,
        event_bus=event_bus,
        redis_url=redis_url,
        adapter=ClaudeStdout(printed.stream),
    ) as task:
        await until(lambda: printed.contains(START_MESSAGE), "the briefing")
        db_conn.execute(
            "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE id=%s",
            (lease["id"],),
        )
        db_conn.commit()
        await asyncio.wait_for(task, timeout=SLOW)

    out = printed.text
    assert f"lease {lease['session_id']} ({lease['id']}) for agent {lease['agent_id']}" in out
    assert "ended: expired" in out
    assert f"Notice ID: impersonation-ended:{lease['id']}" in out
    assert "does not end or cancel any newer takeover" in out


async def test_expired_lease_notifies_host_without_subscribing_or_extending(
    active: ActiveSession,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, _ = active
    printed = Printed()
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    async with running(
        relay_lease(lease, machine_identity),
        database=database,
        event_bus=event_bus,
        redis_url=redis_url,
        adapter=ClaudeStdout(printed.stream),
    ) as task:
        await asyncio.wait_for(task, timeout=SLOW)
    out = printed.text
    assert "ended: expired" in out
    assert START_MESSAGE not in out


async def test_a_released_lease_stops_the_relay_without_saying_anything(
    active: ActiveSession,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, _ = active
    printed = Printed()
    leases.release(database, event_bus, lease["id"], "Done", local_machine=machine_identity.name())
    async with running(
        relay_lease(lease, machine_identity),
        database=database,
        event_bus=event_bus,
        redis_url=redis_url,
        adapter=ClaudeStdout(printed.stream),
    ) as task:
        await asyncio.wait_for(task, timeout=SLOW)
    assert printed.text == ""


async def test_new_message_under_an_outstanding_batch_is_pushed_as_a_new_batch(
    active: ActiveSession,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, first = active
    printed = Printed()
    async with running(
        relay_lease(lease, machine_identity),
        database=database,
        event_bus=event_bus,
        redis_url=redis_url,
        adapter=ClaudeStdout(printed.stream),
    ) as task:
        await until(lambda: printed.contains(f"ids={first}"), "the first batch")
        second = insert_inbound_message(
            db_conn,
            lease["agent_id"],
            "A second message",
            "user",
            kind="chat",
            bus=event_bus,
            database=database,
        )
        db_conn.commit()
        await until(lambda: printed.contains(f"ids={second}"), "the second batch")
        leases.release(
            database, event_bus, lease["id"], "Done", local_machine=machine_identity.name()
        )
        await asyncio.wait_for(task, timeout=SLOW)
    batches = printed.text.split("Meiva message agent=")[1:]
    assert len(batches) == 2
    assert f"ids={first}" in batches[0] and f"[id={second}]" not in batches[0]
    assert f"ids={second}" in batches[1] and f"[id={first}]" not in batches[1]


async def test_native_catchup_recovers_message_with_no_redis_publish(
    active: ActiveSession,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, first = active
    printed = Printed()
    async with running(
        relay_lease(lease, machine_identity),
        database=database,
        event_bus=event_bus,
        redis_url=redis_url,
        adapter=ClaudeStdout(printed.stream),
    ) as task:
        await until(lambda: printed.contains(f"ids={first}"), "the first batch")
        quiet = db_conn.execute(
            "INSERT INTO inbound_messages(agent_id,content,kind,source) "
            "VALUES(%s,'No wake was published for this row','system_note','system') RETURNING id",
            (lease["agent_id"],),
        ).fetchone()
        db_conn.commit()
        assert quiet is not None
        await until(lambda: printed.contains(f"[id={quiet[0]}]"), "the catch-up read")
        leases.release(
            database, event_bus, lease["id"], "Done", local_machine=machine_identity.name()
        )
        await asyncio.wait_for(task, timeout=SLOW)


async def test_release_during_debounce_prevents_delivery(
    active: ActiveSession,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, mid = active
    printed = Printed()
    async with running(
        relay_lease(lease, machine_identity),
        database=database,
        event_bus=event_bus,
        redis_url=redis_url,
        adapter=ClaudeStdout(printed.stream),
        tuning=RelayTuning(debounce=1.0, max_chars=2000, min_emit_interval=0.0),
    ) as task:
        await until(lambda: printed.contains(START_MESSAGE), "the briefing")
        leases.release(
            database, event_bus, lease["id"], "Done", local_machine=machine_identity.name()
        )
        await asyncio.wait_for(task, timeout=SLOW)
    assert "Meiva message agent=" not in printed.text
    assert delivery_attempts(db_conn, mid) == 0
    assert_message_pending(db_conn, mid)


async def test_waits_for_native_consent_then_delivers_start_and_inbox(
    lease_at: LeaseAt,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, owner, mid = lease_at(ImpersonationStatus.ACCEPTED)
    printed = Printed()
    async with running(
        relay_lease(lease, machine_identity),
        database=database,
        event_bus=event_bus,
        redis_url=redis_url,
        adapter=ClaudeStdout(printed.stream),
    ) as task:
        await asyncio.sleep(3 * FAST.catchup_seconds)
        assert printed.text == ""
        leases.activate(database, event_bus, lease["id"], owner)
        await until(lambda: printed.contains(f"ids={mid}"), "the inbox after activation")
        leases.release(
            database, event_bus, lease["id"], "Done", local_machine=machine_identity.name()
        )
        await asyncio.wait_for(task, timeout=SLOW)
    assert printed.text.startswith(START_MESSAGE)


@pytest.mark.parametrize("outcome", ["rejected", "expired"])
async def test_waiting_controller_is_told_terminal_outcome(
    outcome: str,
    lease_at: LeaseAt,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, owner, _ = lease_at(ImpersonationStatus.ACCEPTED)
    printed = Printed()
    async with running(
        relay_lease(lease, machine_identity),
        database=database,
        event_bus=event_bus,
        redis_url=redis_url,
        adapter=ClaudeStdout(printed.stream),
    ) as task:
        await asyncio.sleep(2 * FAST.catchup_seconds)
        if outcome == "rejected":
            lease_relay.fail_acceptance(
                database, event_bus, lease["id"], owner, "relay would not start"
            )
        else:
            db_conn.execute(
                "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
                "WHERE id=%s",
                (lease["id"],),
            )
            db_conn.commit()
        await asyncio.wait_for(task, timeout=SLOW)
    out = printed.text
    assert f"ended: {outcome}" in out
    assert START_MESSAGE not in out


async def test_a_codex_refusal_stops_the_relay_and_keeps_the_inbox(
    active: ActiveSession,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, mid = active
    with short_socket_path() as socket_path, codex_app_server(socket_path, [False]) as host:
        adapter = CodexSteer(THREAD, host.endpoint)
        async with running(
            relay_lease(lease, machine_identity),
            database=database,
            event_bus=event_bus,
            redis_url=redis_url,
            adapter=adapter,
        ) as task:
            with pytest.raises(
                RuntimeError, match=r"Codex Steer delivery failed.*ActiveTurnNotSteerable"
            ):
                await asyncio.wait_for(task, timeout=SLOW)
    assert len(host.turns) == 1
    assert host.texts[0].startswith(START_MESSAGE)
    assert_message_pending(db_conn, mid)


async def test_a_codex_relay_steers_the_thread_with_literal_text(
    active: ActiveSession,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, _ = active
    literal = "Meiva push with literal $(no-shell) and `no-shell`"
    insert_inbound_message(
        db_conn, lease["agent_id"], literal, "user", kind="chat", bus=event_bus, database=database
    )
    db_conn.commit()
    with short_socket_path() as socket_path, codex_app_server(socket_path, [True]) as host:
        async with running(
            relay_lease(lease, machine_identity),
            database=database,
            event_bus=event_bus,
            redis_url=redis_url,
            adapter=CodexSteer(THREAD, host.endpoint),
        ) as task:
            await until(lambda: len(host.turns) >= 2, "the briefing and the batch")
            leases.release(
                database, event_bus, lease["id"], "Done", local_machine=machine_identity.name()
            )
            await asyncio.wait_for(task, timeout=SLOW)
    assert {thread for thread, _ in host.turns} == {str(THREAD)}
    assert host.texts[0].startswith(START_MESSAGE)
    assert literal in host.texts[1]


async def test_an_antigravity_relay_posts_send_agent_message_with_the_recorded_csrf_token(
    active: ActiveSession,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, mid = active
    with antigravity_server() as host:
        server = AntigravityServer(address=host.address, csrf_token=CSRF)
        async with running(
            relay_lease(lease, machine_identity),
            database=database,
            event_bus=event_bus,
            redis_url=redis_url,
            adapter=AntigravitySend(THREAD, server),
        ) as task:
            await until(lambda: len(host.posts) >= 2, "the briefing and the batch")
            leases.release(
                database, event_bus, lease["id"], "Done", local_machine=machine_identity.name()
            )
            await asyncio.wait_for(task, timeout=SLOW)
    assert {post.path for post in host.posts} == {
        "/exa.language_server_pb.LanguageServerService/SendAgentMessage"
    }
    assert {post.csrf_token for post in host.posts} == {CSRF}
    assert {post.body["recipient"] for post in host.posts} == {str(THREAD)}
    assert host.texts[0].startswith(START_MESSAGE)
    assert f"[id={mid}]" in host.texts[1]


async def test_an_antigravity_relay_whose_server_is_gone_fails_the_briefing_loudly(
    active: ActiveSession,
    unused_tcp_port: int,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, _ = active
    gone = AntigravityServer(address=f"127.0.0.1:{unused_tcp_port}", csrf_token=CSRF)
    async with running(
        relay_lease(lease, machine_identity),
        database=database,
        event_bus=event_bus,
        redis_url=redis_url,
        adapter=AntigravitySend(THREAD, gone),
    ) as task:
        with pytest.raises(RuntimeError, match="Antigravity SendAgentMessage delivery failed"):
            await asyncio.wait_for(task, timeout=SLOW)
