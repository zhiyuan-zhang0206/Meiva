# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
# meiva:note
# The `ops` op clusters: ops-server-callable RPC implementations.
#
# Free functions backing both the gateway FastAPI handlers and the in-process dispatch in
# services/agent_runner/agent_ops/daemon.py. These tests pin the contract independently of either
# entry point: dispatch routing in the ops server has its own coverage in
# services/agent_runner/agent_ops/tests/test_daemon.py, endpoint smoke tests live in
# gateway/tests/routers/cluster_endpoints/test_cluster_routes.py.

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg_pool import ConnectionPool
from pydantic import ValidationError
from redis.client import PubSub

from base.agents.messages.inbound import InboundKind
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.proc import run_bounded
from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog
from base.packages.tests.model_catalog import inert_keys
from ops import lifecycle
from ops.lifecycle import launch
from ops.rpc_schemas import (
    RestartAgentRequest,
    ResurrectAgentRequest,
    SpawnAgentRequest,
    TerminateAgentRequest,
)


def db_from_gate(*, database_gate: ProcessDbGate) -> Database:
    return Database.from_settings(gate=database_gate)


class TestSpawnAgentRequestSourceValidation:
    """F1: an illegal prompt_source must be rejected at the schema boundary,
    not silently accepted and deferred to the agent claim node — where an
    unrecognized envelope source raises ValueError and kills the just-spawned
    process. The schema reuses base.agents.messages.envelope.validate_source so the legal set
    stays single-sourced with the claim-side wrap."""

    def test_rejects_unrecognized_source(self) -> None:
        # The old `ui:` channel prefix is gone — it must now fail the boundary.
        with pytest.raises(ValidationError):
            SpawnAgentRequest(prompt="hi", prompt_source="ui:web")

    def test_accepts_user_source(self) -> None:
        body = SpawnAgentRequest(prompt="hi", prompt_source="user")
        assert body.prompt_source == "user"

    def test_accepts_agent_source(self) -> None:
        body = SpawnAgentRequest(prompt="hi", prompt_source="agent:3")
        assert body.prompt_source == "agent:3"

    def test_no_source_validation_without_prompt(self) -> None:
        # prompt_source is only meaningful alongside a prompt; a spawn without a
        # prompt (fork / blank agent) carries no source to validate.
        body = SpawnAgentRequest(spawner="user")
        assert body.prompt_source is None


class TestRestartAgentRequestConfigOverlay:
    """Restart overlays fail at both HTTP and runner schema boundaries."""

    @pytest.mark.parametrize("profile", ["gateway", "runner"])
    def test_validates_overlay_in_boundary_profile(self, profile: str, tmp_path: Path) -> None:
        child = Path(__file__).with_name("_restart_profile_child.py")
        result = run_bounded(
            [sys.executable, str(child), profile],
            timeout=20,
            capture_output=True,
            text=True,
            env={
                "MEIVA_HOME": str(tmp_path),
                "MEIVA_CONFIG_FETCH": "skip",
                "MEIVA_PROCESS_PROFILE": profile,
                "MEIVA_CONFIG_BOOT": "eager",
            },
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == profile

    @pytest.mark.parametrize(
        "config_overlay",
        [
            {"definitely_not_a_config_field": "x"},
            {"heartbeat_pause_max_seconds": "not-a-number"},
            {"reasoning_effort": "turbo"},
        ],
    )
    def test_rejects_invalid_overlay(self, config_overlay: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            RestartAgentRequest(config_overlay=config_overlay)

    @pytest.mark.parametrize("config_overlay", [None, {}])
    def test_accepts_legacy_empty_overlay_forms(
        self, config_overlay: dict[str, object] | None
    ) -> None:
        body = RestartAgentRequest(config_overlay=config_overlay)
        assert body.config_overlay == config_overlay


@pytest.fixture
def stub_pool() -> MagicMock:
    """A pool test double: mocked operation helpers never access it."""
    return MagicMock()


class TestSpawnPrechecksBlocking:
    def test_fork_resolves_checkpoint(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        lifecycle_agent_id: int,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        db_conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint, metadata) "
            "VALUES (%s, 'ckpt:v1', '{}'::jsonb, '{}'::jsonb)",
            (str(lifecycle_agent_id),),
        )
        db_conn.commit()
        checkpoint = launch.spawn_prechecks_blocking(
            SpawnAgentRequest(spawner="user", fork_from=lifecycle_agent_id),
            db_pool,
            catalog=model_catalog,
            keys=inert_keys(model_catalog),
            authority=config_authority,
        )
        assert checkpoint == "ckpt:v1"

    def test_fork_empty_raises(
        self,
        db_pool: ConnectionPool,
        lifecycle_agent_id: int,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        from base.api_contracts.agent_contract import ForkSourceEmpty

        with pytest.raises(ForkSourceEmpty):
            launch.spawn_prechecks_blocking(
                SpawnAgentRequest(spawner="user", fork_from=lifecycle_agent_id),
                db_pool,
                catalog=model_catalog,
                keys=inert_keys(model_catalog),
                authority=config_authority,
            )

    def test_plain_spawn_needs_no_checkpoint(
        self,
        db_pool: ConnectionPool,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        checkpoint = launch.spawn_prechecks_blocking(
            SpawnAgentRequest(spawner="user"),
            db_pool,
            catalog=model_catalog,
            keys=inert_keys(model_catalog),
            authority=config_authority,
        )
        assert checkpoint is None


async def test_restart_agent_op_terminated_short_circuits(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    from base.agents.tests.units import spawn_agent
    from ops.tests.pool_support import make_test_pool

    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent_id,))
    db_conn.commit()
    with (
        make_test_pool() as pool,
        event_bus.sync_redis() as redis,
        PubSub(redis.connection_pool) as received,
    ):
        received.subscribe(event_bus.channel)
        subscribed = cast(dict[str, object] | None, received.get_message(timeout=2))
        assert subscribed is not None and subscribed["type"] == "subscribe"
        resp = await lifecycle.restart_agent_op(
            database,
            event_bus,
            agent_id,
            RestartAgentRequest(source="user"),
            pool,
            catalog=build_model_catalog(),
        )
        assert received.get_message(timeout=0.1) is None
    assert resp.status == "already_terminated"
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='restart'", (agent_id,)
    ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_restart_lifecycle_op_validates_overlay_on_the_runner(
    stub_pool: MagicMock,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """The runner reparses forwarded restart bodies before any DB write."""
    with pytest.raises(ValidationError):
        await lifecycle.lifecycle_op(
            database,
            event_bus,
            "/api/agents/9/restart",
            {"config_overlay": {"definitely_not_a_config_field": "x"}},
            stub_pool,
            catalog=build_model_catalog(),
        )


@pytest.mark.asyncio
async def test_resurrect_agent_op_alive_returns_already_alive(
    lifecycle_agent_id: int,
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
) -> None:
    resp = await lifecycle.resurrect_agent_op(
        database,
        event_bus,
        lifecycle_agent_id,
        ResurrectAgentRequest(prompt="test"),
    )
    assert resp.status == "already_alive"
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (lifecycle_agent_id,)
    ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_resurrect_agent_op_stale_trigger_returns_idempotent_noop(
    lifecycle_agent_id: int,
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
) -> None:
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (lifecycle_agent_id,))
    db_conn.commit()
    resp = await lifecycle.resurrect_agent_op(
        database,
        event_bus,
        lifecycle_agent_id,
        ResurrectAgentRequest(resurrected_by="system"),
        trigger_inbound_id=123,
        trigger_inbound_kind=InboundKind.CHAT,
    )
    assert resp.status == "already_alive"
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (lifecycle_agent_id,)
    ).fetchone() == ("terminated",)


@pytest.mark.asyncio
async def test_terminate_agent_op_terminated_short_circuits(
    lifecycle_agent_id: int,
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
) -> None:
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (lifecycle_agent_id,))
    db_conn.commit()

    def no_kill(agent_id: int, *, before_session_index: int) -> list[int]:
        raise AssertionError("a terminate without the option never kills shell sessions")

    async def no_cancel(agent_id: int) -> None:
        raise AssertionError("an already terminated agent has no turn to cancel")

    resp = await lifecycle.terminate_agent_op(
        database,
        event_bus,
        lifecycle_agent_id,
        TerminateAgentRequest(),
        db_pool,
        cancel_turn=no_cancel,
        kill_shells=no_kill,
    )
    assert resp.status == "already_terminated"
    assert resp.shell_sessions is None
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (lifecycle_agent_id,)
    ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_lifecycle_op_parses_path_to_terminate(
    lifecycle_agent_id: int,
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    model_catalog: ModelCatalog,
) -> None:
    result = await lifecycle.lifecycle_op(
        database,
        event_bus,
        f"/api/agents/{lifecycle_agent_id}/terminate",
        {"source": "user"},
        db_pool,
        catalog=model_catalog,
    )
    assert result.status == "enqueued"
    assert db_conn.execute(
        "SELECT kind, source, status FROM inbound_messages WHERE agent_id=%s",
        (lifecycle_agent_id,),
    ).fetchall() == [("terminate", "user", "pending")]
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (lifecycle_agent_id,)
    ).fetchone() == ("idling",)


@pytest.mark.asyncio
async def test_lifecycle_op_unparseable_path_raises(
    stub_pool: MagicMock,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    with pytest.raises(ValueError, match="lifecycle path not recognized"):
        await lifecycle.lifecycle_op(
            database,
            event_bus,
            "/api/agents/bogus",
            {},
            stub_pool,
            catalog=build_model_catalog(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_guarded_resurrect_path_requires_trigger(
    stub_pool: MagicMock,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """The new internal path fails closed when its CAS evidence is missing."""
    with pytest.raises(ValueError, match="requires trigger inbound"):
        await lifecycle.lifecycle_op(
            database,
            event_bus,
            "/api/agents/42/resurrect-if-pending-work",
            {"resurrected_by": "system"},
            stub_pool,
            catalog=build_model_catalog(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_manual_lifecycle_path_rejects_auto_resurrect_trigger(
    stub_pool: MagicMock,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """A mismatched path/guard pair cannot silently fall back to an
    unconditional manual resurrect."""
    with pytest.raises(ValueError, match="only valid for resurrect-if-pending-work"):
        await lifecycle.lifecycle_op(
            database,
            event_bus,
            "/api/agents/42/resurrect-explicit",
            {"resurrected_by": "system"},
            stub_pool,  # type: ignore[arg-type]
            trigger_inbound_id=99,
            trigger_inbound_kind=InboundKind.CHAT,
            catalog=build_model_catalog(),
        )


@pytest.mark.asyncio
async def test_explicit_resurrect_dispatches_manual_op(
    lifecycle_agent_id: int,
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    model_catalog: ModelCatalog,
) -> None:
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (lifecycle_agent_id,))
    db_conn.commit()
    result = await lifecycle.lifecycle_op(
        database,
        event_bus,
        f"/api/agents/{lifecycle_agent_id}/resurrect-explicit",
        {"resurrected_by": "user"},
        db_pool,
        catalog=model_catalog,
    )
    assert result.status == "spawned"
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (lifecycle_agent_id,)
    ).fetchone() == ("idling",)
    assert db_conn.execute(
        "SELECT source FROM audit_events WHERE agent_id=%s AND event_name='resurrect'",
        (lifecycle_agent_id,),
    ).fetchall() == [("user",)]
