from __future__ import annotations

from typing import cast
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import ConnectionPool
from redis.client import PubSub

from base.agents.birth.harness import LAUNCHABLE_HARNESSES
from base.agents.birth.spawn import create_agent_row
from base.api_contracts.agent_contract import Harness
from base.cluster import inbound_channel
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.events.live.bus import EventBus
from base.packages.plugin_providers import build_model_catalog
from ops import lifecycle
from ops.lifecycle import launch
from ops.rpc_schemas import LaunchAgentRequest


def test_a_harness_without_a_launcher_is_refused(
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    agent_id, _birth_config, _inbound_id, attempt = create_agent_row(
        database,
        event_bus,
        machine=machine_identity.name(),
        harness="antigravity",
        catalog=build_model_catalog(),
        authority=config_authority,
    )
    pool: ConnectionPool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=1)
    body = LaunchAgentRequest(agent_id=agent_id, launch_attempt_id=attempt)
    # meiva:note
    # The shipped set grows as launchers land, so the refusal is driven through the
    # parameter rather than by betting on which harnesses still lack one: antigravity
    # was this test's example until the release that gave it a launcher.
    # meiva:link base/agents/docs/harness/harness.meiva.okf.md
    without_it = frozenset(LAUNCHABLE_HARNESSES - {Harness.ANTIGRAVITY})
    with pool, pytest.raises(ValueError, match="no launcher yet"):
        launch.validate_launch_row(
            pool, body, local_machine=machine_identity.name(), launchable=without_it
        )
    assert db_conn.execute(
        "SELECT status, harness FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchall() == [("idling", "antigravity")]


@pytest.mark.asyncio
async def test_new_launch_attempt_is_repeatable_without_prompt_insertion(
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    agent_id, birth_config, inbound_id, attempt = create_agent_row(
        database,
        event_bus,
        machine=machine_identity.name(),
        prompt="one goal",
        prompt_source="user",
        catalog=build_model_catalog(),
        authority=config_authority,
    )
    body = LaunchAgentRequest(
        agent_id=agent_id, launch_attempt_id=attempt, birth_config=birth_config
    )
    pool: ConnectionPool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=1)
    with (
        pool,
        event_bus.sync_redis(decode_responses=True) as redis,
        PubSub(redis.connection_pool) as received,
    ):
        received.subscribe(inbound_channel(agent_id, event_bus.channel))
        subscribed = cast(dict[str, object] | None, received.get_message(timeout=2))
        assert subscribed is not None and subscribed["type"] == "subscribe"
        await lifecycle.launch_agent_op(
            database,
            event_bus,
            body,
            pool,
            catalog=build_model_catalog(),
            local_machine=machine_identity.name(),
        )
        await lifecycle.launch_agent_op(
            database,
            event_bus,
            body,
            pool,
            catalog=build_model_catalog(),
            local_machine=machine_identity.name(),
        )
        stale = body.model_copy(update={"launch_attempt_id": uuid4()})
        with pytest.raises(ValueError, match="stale or misplaced"):
            await lifecycle.launch_agent_op(
                database,
                event_bus,
                stale,
                pool,
                catalog=build_model_catalog(),
                local_machine=machine_identity.name(),
            )
        db_conn.execute("UPDATE agents_meta SET machine='later-placement' WHERE id=%s", (agent_id,))
        db_conn.commit()
        with pytest.raises(ValueError, match="stale or misplaced"):
            await lifecycle.launch_agent_op(
                database,
                event_bus,
                body,
                pool,
                catalog=build_model_catalog(),
                local_machine=machine_identity.name(),
            )
        for _ in range(2):
            wake = cast(dict[str, object] | None, received.get_message(timeout=2))
            assert wake is not None and wake["data"] == "0"
        assert received.get_message(timeout=0.1) is None
    assert db_conn.execute(
        "SELECT id, content, status FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchall() == [(inbound_id, "one goal", "pending")]
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling",)
