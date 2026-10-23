# meiva:note
# Force cancellation follows commit; failed launch validation preserves birth state.

from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops import lifecycle
from ops.rpc_schemas import LaunchAgentRequest, TerminateAgentRequest


async def test_force_terminate_hosted_commits_before_cancelling_turn(
    lifecycle_agent_id: int,
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    cancelled: list[int] = []

    async def cancel(agent_id: int) -> None:
        assert db_conn.execute(
            "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone() == ("terminated",)
        db_conn.commit()
        cancelled.append(agent_id)

    def no_shell_kill(agent_id: int, *, before_session_index: int) -> list[int]:
        raise AssertionError("force without the shell option must not kill shell sessions")

    resp = await lifecycle.terminate_agent_op(
        database,
        event_bus,
        lifecycle_agent_id,
        TerminateAgentRequest(force=True),
        db_pool,
        cancel_turn=cancel,
        kill_shells=no_shell_kill,
    )
    assert resp.status == "enqueued"
    assert resp.shell_sessions is None
    assert cancelled == [lifecycle_agent_id]
    assert db_conn.execute(
        "SELECT kind, status FROM inbound_messages WHERE agent_id=%s", (lifecycle_agent_id,)
    ).fetchall() == [("terminate", "done")]


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
