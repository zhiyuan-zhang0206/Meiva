# meiva:note
# Force cancellation follows commit; failed launch validation preserves birth state.

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import MachineIdentity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops import lifecycle
from ops.lifecycle.hosted_turn import LocalAgentHost
from ops.lifecycle.tests.loopback.agent_host import serve_agent_host
from ops.lifecycle.tests.loopback.scripted_post import closed_port
from ops.rpc_schemas import LaunchAgentRequest, TerminateAgentRequest


# meiva:note
# The agent host is asked to cancel the turn only after the termination is committed: its cancel
# route reads the agent row over its own connection, the way the host's turn would see it.
async def test_force_terminate_hosted_commits_before_cancelling_turn(
    lifecycle_agent_id: int,
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    unit_home: Path,
) -> None:
    statuses_seen_by_the_host: list[tuple[object, ...] | None] = []

    def host_reads_the_row(agent_id: int) -> None:
        with psycopg.connect(settings.data_plane.db_url) as probe:
            statuses_seen_by_the_host.append(
                probe.execute("SELECT status FROM agents_meta WHERE id=%s", (agent_id,)).fetchone()
            )

    with serve_agent_host(unit_home, on_cancel=host_reads_the_row) as served:
        resp = await lifecycle.terminate_agent_op(
            database,
            event_bus,
            lifecycle_agent_id,
            TerminateAgentRequest(force=True),
            db_pool,
            host=served.host,
        )
    assert resp.status == "enqueued"
    assert resp.shell_sessions is None
    assert served.cancel_requests == [lifecycle_agent_id]
    assert statuses_seen_by_the_host == [("terminated",)]
    assert db_conn.execute(
        "SELECT kind, status FROM inbound_messages WHERE agent_id=%s", (lifecycle_agent_id,)
    ).fetchall() == [("terminate", "done")]


# meiva:note
# The cancel call is a best-effort accelerator: a host that cannot be reached leaves the committed
# force termination and its response unchanged.
async def test_force_terminate_survives_an_unreachable_agent_host(
    lifecycle_agent_id: int,
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    unit_home: Path,
) -> None:
    host = LocalAgentHost(home=unit_home, health_port=closed_port())

    resp = await lifecycle.terminate_agent_op(
        database,
        event_bus,
        lifecycle_agent_id,
        TerminateAgentRequest(force=True),
        db_pool,
        host=host,
    )

    assert resp.status == "enqueued"
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (lifecycle_agent_id,)
    ).fetchone() == ("terminated",)


@pytest.mark.asyncio
async def test_launch_agent_op_hosted_validation_failure_preserves_its_row(
    lifecycle_agent_id: int,
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    machine_identity: MachineIdentity,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    before = db_conn.execute(
        "SELECT status, birth_config, termination_source FROM agents_meta WHERE id=%s",
        (lifecycle_agent_id,),
    ).fetchone()
    db_conn.commit()
    assert before is not None
    body = LaunchAgentRequest(
        agent_id=lifecycle_agent_id,
        launch_attempt_id=uuid4(),
        birth_config=before[1],
        config={"llm_model": "not-a-registered-launch-model"},
    )
    with pytest.raises(ValueError, match="not-a-registered-launch-model"):
        await lifecycle.launch_agent_op(
            database,
            event_bus,
            body,
            db_pool,
            catalog=model_catalog,
            authority=config_authority,
            local_machine=machine_identity.name(),
        )
    assert (
        db_conn.execute(
            "SELECT status, birth_config, termination_source FROM agents_meta WHERE id=%s",
            (lifecycle_agent_id,),
        ).fetchone()
        == before
    )
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (lifecycle_agent_id,)
    ).fetchone() == (0,)
