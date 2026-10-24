# meiva:note
# The relay follows the delivery budget its lease snapshotted, across a restart: a real lease,
# the real Redis wake listener, a Codex app server on a unix socket, and the database clock
# aged with SQL to cross each ACK window without waiting for it.

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.impersonation.status import ImpersonationStatus
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.events.live.bus import EventBus
from cli.commands.agents.impersonation_adapters import CodexSteer
from cli.commands.agents.impersonation_relay import RelayLease
from cli.commands.agents.tests.lease_support import (
    ActiveSession,
    assert_message_pending,
    delivery_attempts,
    elapse,
)
from cli.commands.agents.tests.lease_support import active as active
from cli.commands.agents.tests.loopback_hosts import codex_app_server, short_socket_path
from cli.commands.agents.tests.relay_harness import (
    FAST,
    SLOW,
    THREAD,
    relay_lease,
    running,
    until,
)


# meiva:note
# The resources one budget case drives: the lease and its pending message, the relay's credential
# for it, and the database, bus and Redis the relay serves it from.
@dataclass(frozen=True)
class World:
    lease: dict[str, Any]
    message_id: int
    relay: RelayLease
    db_conn: psycopg.Connection
    database: Database
    event_bus: EventBus
    redis_url: str

    @property
    def window(self) -> int:
        return self.lease["ack_window_seconds"]

    @property
    def attempts(self) -> int:
        return self.lease["max_delivery_attempts"]

    def pushes_of_the_message(self, texts: list[str]) -> list[str]:
        return [text for text in texts if f"[id={self.message_id}]" in text]


# meiva:note
# A relay whose host accepts the briefing and refuses the first push dies with the refusal, and
# the push it died on still spent an attempt. Returns what the host was sent.
async def _relay_that_crashes_on_its_first_push(world: World, socket_path: Path) -> list[str]:
    with codex_app_server(socket_path, [True, False]) as host:
        adapter = CodexSteer(THREAD, host.endpoint)
        async with running(
            world.relay,
            database=world.database,
            event_bus=world.event_bus,
            redis_url=world.redis_url,
            adapter=adapter,
        ) as crashed:
            with pytest.raises(RuntimeError, match="Codex Steer delivery failed"):
                await asyncio.wait_for(crashed, timeout=SLOW)
    assert delivery_attempts(world.db_conn, world.message_id) == 1
    return host.texts


# meiva:note
# A relay whose host accepts everything pushes the message once per attempt the lease allows,
# crossing each ACK window with SQL; after the last attempt it stays quiet while the lease
# stays active, until the controller ends it. Returns what the host was sent.
async def _relay_until_the_budget_is_spent(
    world: World, socket_path: Path, already_pushed: list[str]
) -> list[str]:
    with codex_app_server(socket_path, [True]) as host:
        adapter = CodexSteer(THREAD, host.endpoint)
        async with running(
            world.relay,
            database=world.database,
            event_bus=world.event_bus,
            redis_url=world.redis_url,
            adapter=adapter,
        ) as task:

            def delivered() -> int:
                return len(world.pushes_of_the_message(already_pushed + host.texts))

            async def reached(count: int) -> None:
                await until(lambda: delivered() >= count, f"delivery attempt {count}")

            for pushed in range(1, world.attempts + 1):
                await reached(pushed)
                elapse(world.db_conn, world.lease, world.window + 1)
            await asyncio.sleep(4 * FAST.catchup_seconds)
            assert delivered() == world.attempts
            status = leases.relay_get(
                world.database,
                world.event_bus,
                world.lease["id"],
                world.lease["relay_token"],
                local_machine=world.relay.machine,
            )["status"]
            assert status == ImpersonationStatus.ACTIVE
            leases.release(
                world.database,
                world.event_bus,
                world.lease["id"],
                "Explicit test end after delivery exhaustion",
                local_machine=world.relay.machine,
            )
            await asyncio.wait_for(task, timeout=SLOW)
    return host.texts


@pytest.mark.parametrize("active", [(180, 2), (7, 1), (60, 3)], indirect=True)
@pytest.mark.parametrize("crash_after_first_submission", [False, True])
async def test_real_relay_uses_snapshotted_config_across_restart(
    active: ActiveSession,
    crash_after_first_submission: bool,
    unit_home: Path,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    lease, _, mid = active
    world = World(
        lease, mid, relay_lease(lease, machine_identity), db_conn, database, event_bus, redis_url
    )
    # meiva:note
    # Later config edits cannot change a lease's promised deadlines or budget.
    (unit_home / ".env").write_text(
        "MEIVA_IMPERSONATION_ACK_WINDOW_SECONDS=999\nMEIVA_IMPERSONATION_MAX_DELIVERY_ATTEMPTS=9\n"
    )
    texts: list[str] = []
    with short_socket_path() as socket_path:
        if crash_after_first_submission:
            texts += await _relay_that_crashes_on_its_first_push(world, socket_path)
            elapse(db_conn, lease, world.window + 1)
        texts += await _relay_until_the_budget_is_spent(world, socket_path, texts)

    deliveries = world.pushes_of_the_message(texts)
    assert len(deliveries) == world.attempts
    for count, push in enumerate(deliveries, 1):
        assert f"ACK within {world.window}s" in push
        assert f"Delivery attempt {count}/{world.attempts}" in push
        assert ("final delivery" in push) == (count == world.attempts)
    assert f"Each message has {world.attempts} delivery attempts" in texts[0]
    assert f"with {world.window}s to ACK each" in texts[0]
    assert all("Meiva control expired" not in text for text in texts)
    assert_message_pending(db_conn, mid)
