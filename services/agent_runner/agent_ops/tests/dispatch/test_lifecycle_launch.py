# meiva:note
# The launch and lifecycle operations run against a real agent row and a real wake channel.

from __future__ import annotations

from uuid import uuid4

import pytest

from base.agents.birth.spawn import create_agent_row
from base.api_contracts.launch_retry import RetryLaunchRequest
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.events.live.bus import EventBus
from base.packages.plugin_providers import build_model_catalog
from ops.agents.launch_retry import accept_retry_launch
from ops.cluster import rpc
from ops.rpc_schemas import LaunchAgentRequest
from services.agent_runner.agent_ops.tests.ops_support import Ops, watching_wakes


@pytest.fixture
def launch(
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> LaunchAgentRequest:
    """A freshly created agent row and the launch attempt its creation committed."""
    agent_id, birth_config, _inbound_id, attempt = create_agent_row(
        database,
        event_bus,
        machine=machine_identity.name(),
        prompt="one goal",
        prompt_source="user",
        catalog=build_model_catalog(),
        authority=config_authority,
    )
    return LaunchAgentRequest(
        agent_id=agent_id, launch_attempt_id=attempt, birth_config=birth_config
    )


async def test_spawn_launch_wakes_the_agent_and_answers_its_id(
    ops: Ops, event_bus: EventBus, launch: LaunchAgentRequest
) -> None:
    with watching_wakes(event_bus, launch.agent_id) as wakes:
        status, result = await ops.dispatch("spawn-launch", launch.model_dump(mode="json"))

        assert (status, result) == ("completed", {"id": launch.agent_id})
        assert wakes.received() == 1


async def test_spawn_launch_of_a_stale_attempt_fails_without_waking(
    ops: Ops, event_bus: EventBus, launch: LaunchAgentRequest
) -> None:
    stale = launch.model_copy(update={"launch_attempt_id": uuid4()})

    with watching_wakes(event_bus, launch.agent_id) as wakes:
        status, result = await ops.dispatch("spawn-launch", stale.model_dump(mode="json"))

        assert status == "failed"
        assert "stale or misplaced" in str(result["error"])
        assert wakes.received(wait_s=0.3) == 0


async def test_launch_reconcile_repeats_its_wake_through_its_own_handler(
    ops: Ops, event_bus: EventBus, launch: LaunchAgentRequest
) -> None:
    _machine, accepted = accept_retry_launch(
        ops.pool,
        "reconcile-key",
        launch.agent_id,
        RetryLaunchRequest(expected_prior_attempt_id=launch.launch_attempt_id),
    )
    payload = {"launch_attempt_id": str(accepted.launch_attempt_id)}

    with watching_wakes(event_bus, launch.agent_id) as wakes:
        first = await ops.dispatch("launch-reconcile", payload)
        second = await ops.dispatch("launch-reconcile", payload)

        assert first == second == ("completed", {"wake_published": True})
        assert wakes.received() == 2
    assert "launch-reconcile" not in rpc.NON_IDEMPOTENT_KINDS
    assert {"spawn-launch", "lifecycle"} <= rpc.NON_IDEMPOTENT_KINDS


async def test_launch_reconcile_without_a_receipt_fails(ops: Ops) -> None:
    status, result = await ops.dispatch("launch-reconcile", {"launch_attempt_id": str(uuid4())})

    assert status == "failed"
    assert "receipt is missing" in str(result["error"])


async def test_lifecycle_enqueues_the_termination_of_a_real_agent(
    ops: Ops, launch: LaunchAgentRequest
) -> None:
    status, result = await ops.dispatch(
        "lifecycle", {"path": f"/api/agents/{launch.agent_id}/terminate", "body": {}}
    )

    assert (status, result) == (
        "completed",
        {"status": "enqueued", "open_tasks": None, "shell_sessions": None},
    )


async def test_a_lifecycle_error_carries_its_wire_reason(ops: Ops) -> None:
    status, result = await ops.dispatch(
        "lifecycle", {"path": "/api/agents/999999/terminate", "body": {}}
    )

    assert status == "failed"
    assert "AgentNotFound" in str(result["error"])
    assert result["reason"] == "agent_not_found"


async def test_lifecycle_without_a_path_fails_on_validation(ops: Ops) -> None:
    status, result = await ops.dispatch("lifecycle", {})

    assert status == "failed"
    assert "path" in str(result["error"])


async def test_an_unparseable_lifecycle_path_fails(ops: Ops) -> None:
    status, result = await ops.dispatch("lifecycle", {"path": "/garbage", "body": {}})

    assert status == "failed"
    assert "not recognized" in str(result["error"])
