# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
"""The `ops` op clusters — ops-server-callable RPC implementations.

Free functions backing both the gateway FastAPI handlers and the in-process dispatch in
services/agent_runner/agent_ops/daemon.py. These tests pin the contract independently of either entry point:
dispatch routing in the ops server has its own coverage in services/agent_runner/agent_ops/tests/test_daemon.py,
endpoint smoke tests live in tests/components/gateway/test_cluster_endpoints.py.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.packages.plugin_providers import build_model_catalog
from ops import lifecycle
from ops.rpc_schemas import (
    LaunchAgentRequest,
    TerminateAgentRequest,
)


def _db(*, database_gate: ProcessDbGate) -> Database:
    return Database.from_settings(gate=database_gate)


@pytest.fixture
def stub_pool() -> object:
    """Sentinel pool — every op call below mocks the gateway/agents helpers so
    the pool is never touched, but the signature still requires an object."""
    return object()


# meiva:note
# Hosted force-terminate: no process to SIGKILL — the terminated row is written in
#     one transaction and the turn cancel is asked of the host after it commits.
async def test_force_terminate_hosted_skips_process_kill_and_cancels_turn(
    stub_pool: MagicMock, database: Database, event_bus: EventBus
) -> None:
    from base.api_contracts.agent_contract import AgentStatus

    captured: dict[str, object] = {}

    def _fake_force_blocking(
        _db: object,
        _bus: object,
        aid: int,
        _body: object,
        _pool: object,
    ) -> tuple[AgentStatus, list[str], int, int]:
        captured["agent_id"] = aid
        return AgentStatus.RUNNING, [], 91, 0

    cancelled: list[int] = []

    async def _fake_cancel(aid: int) -> None:
        cancelled.append(aid)

    async def _fake_page_closed(*_a: object, **_k: object) -> None:
        return None

    resp = await lifecycle.terminate_agent_op(
        database,
        event_bus,
        9,
        TerminateAgentRequest(force=True),
        cast(ConnectionPool, stub_pool),
        force_blocking_fn=_fake_force_blocking,
        cancel_turn_fn=_fake_cancel,
        page_closed_fn=_fake_page_closed,
    )
    assert resp.status == "enqueued"
    assert resp.shell_sessions is None
    assert captured == {"agent_id": 9}
    assert cancelled == [9]


@pytest.mark.asyncio
async def test_launch_agent_op_hosted_validation_failure_preserves_its_row(
    stub_pool: MagicMock,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    def _boom_validate(*_a: object, **_k: object) -> None:
        assert _k["config"] == {"llm_model": "deepseek-flash", "reasoning_effort": "max"}
        raise RuntimeError("bad model config")

    body = LaunchAgentRequest(
        agent_id=7,
        launch_attempt_id=UUID("00000000-0000-0000-0000-000000000001"),
        birth_config={"llm_model": "deepseek-flash", "reasoning_effort": "high"},
        config={"reasoning_effort": "max"},
    )
    with pytest.raises(RuntimeError, match="bad model config"):
        await lifecycle.launch_agent_op(
            database,
            event_bus,
            body,
            cast(ConnectionPool, stub_pool),
            catalog=build_model_catalog(),
            local_machine=machine_identity.name(),
            validate_model_fn=_boom_validate,
        )
