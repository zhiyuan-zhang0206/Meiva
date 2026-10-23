from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent import impersonation
from agent.execution.protocol import read_request, write_request
from agent.nodes import END
from agent.state import BaseAgentState
from agent.takeover.gate import LeasePhase
from agent.takeover.relay import RelayWait
from agent.tests.impersonation.handoff_inputs import handoff_notes, takeover_host
from agent.tests.impersonation.lease_support import Cluster, idle_graph, native_context
from agent.tests.impersonation.lease_support import cluster as cluster
from base.agents.impersonation.status import ImpersonationStatus
from base.agents.observation.relay_supervision import RelaySupervision
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from meiva.tests.fixtures.pin_agent import exec_context

HELD_PHASES = ["accepted", "active", "released", "expired"]


@pytest.mark.parametrize(
    ("status", "phase"),
    [
        (ImpersonationStatus.REQUESTED, LeasePhase.REQUESTED),
        (ImpersonationStatus.ACCEPTED, LeasePhase.ACCEPTED),
        (ImpersonationStatus.ACTIVE, LeasePhase.ACTIVE),
        (ImpersonationStatus.RELEASED, LeasePhase.ENDED),
        (ImpersonationStatus.REJECTED, LeasePhase.ENDED),
        (ImpersonationStatus.EXPIRED, LeasePhase.ENDED),
    ],
)
def test_every_persisted_status_reads_as_one_lease_phase(
    status: ImpersonationStatus, phase: LeasePhase
) -> None:
    assert impersonation.lease_phase({"status": status}) is phase
    assert impersonation.lease_phase(None) is None


def test_only_an_ended_lease_without_its_receipt_is_owed_a_handoff() -> None:
    assert impersonation.handoff_owed(None) is False
    ended = {"status": ImpersonationStatus.EXPIRED, "handoff_applied_at": None}
    assert impersonation.handoff_owed(ended) is True
    assert impersonation.handoff_owed({**ended, "handoff_applied_at": "applied"}) is False
    assert impersonation.handoff_owed({**ended, "status": ImpersonationStatus.ACTIVE}) is False


@pytest.mark.parametrize("phase", HELD_PHASES)
async def test_the_claim_gate_parks_the_graph_while_a_lease_holds_the_agent(
    cluster: Cluster, phase: str
) -> None:
    owner = cluster.native_agent()
    cluster.lease(owner, provider="claude", phase=phase)
    decision = await impersonation.claim_gate(
        BaseAgentState(), owner.agent_id, native_context(cluster, owner)
    )
    assert decision is not None
    assert decision.goto == END
    assert decision.update == {"turn_active": False, "turn_idle": True}


async def test_the_claim_gate_lets_an_agent_without_a_lease_proceed(cluster: Cluster) -> None:
    owner = cluster.native_agent()
    assert (
        await impersonation.claim_gate(
            BaseAgentState(), owner.agent_id, native_context(cluster, owner)
        )
        is None
    )


async def test_the_claim_gate_prepares_a_request_without_a_model_decision(
    cluster: Cluster,
) -> None:
    owner = cluster.native_agent()
    requested = cluster.lease(owner, provider="claude", phase="requested")
    decision = await impersonation.claim_gate(
        BaseAgentState(), owner.agent_id, native_context(cluster, owner)
    )
    assert decision is not None
    assert decision.goto == END
    update = decision.update
    assert isinstance(update, dict)
    assert [message.id for message in cast("list[HumanMessage]", update["messages"])] == [
        "impersonation-introduction",
        f"impersonation-start:{owner.agent_id}:0",
    ]
    assert update["impersonation_introduced"] is True
    assert update["turn_idle"] is True
    assert cluster.get(requested["id"])["status"] == "accepted"


async def _hook_result(
    cluster: Cluster, owner: RuntimeIncarnation, ops_pool: AsyncConnectionPool[Any]
) -> Command[Any]:
    async def hook(*_args: object) -> Command[Any]:
        return Command(goto="llm")

    return await impersonation.protect_native_hooks(hook)(
        BaseAgentState(),
        Runtime(context=native_context(cluster, owner, ops_pool=ops_pool)),
        {"configurable": {"thread_id": str(owner.agent_id)}},
    )


@pytest.mark.parametrize("phase", HELD_PHASES)
async def test_hooks_do_not_run_while_a_lease_holds_the_agent(
    cluster: Cluster, aops_pool: AsyncConnectionPool[Any], phase: str
) -> None:
    owner = cluster.native_agent()
    cluster.lease(owner, provider="claude", phase=phase)
    result = await _hook_result(cluster, owner, aops_pool)
    assert result.goto == END
    assert result.update == {"turn_idle": True}


@pytest.mark.parametrize("phase", [None, "requested"])
async def test_hooks_run_with_no_lease_or_only_a_request(
    cluster: Cluster, aops_pool: AsyncConnectionPool[Any], phase: str | None
) -> None:
    owner = cluster.native_agent()
    if phase is not None:
        cluster.lease(owner, provider="claude", phase=phase)
    result = await _hook_result(cluster, owner, aops_pool)
    assert result.goto == "llm"


def test_exec_envelope_carries_parent_incarnation(tmp_path: Path) -> None:
    incarnation = RuntimeIncarnation(42, uuid4(), uuid4())
    path = tmp_path / "request.json"
    write_request(
        path,
        code="pass",
        context=exec_context(42).describe(),
        timeout_s=10,
        state={},
        incarnation=incarnation,
    )
    assert read_request(path).incarnation == incarnation


async def test_settlement_rolls_the_lease_back_when_the_controller_relay_is_not_running(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="claude")
    graph, config = await idle_graph(owner.agent_id)
    quick = replace(takeover_host(tmp_path, cluster.machine), wait=RelayWait(0.0, 0.01))
    assert not await impersonation.settle_checkpoint(
        graph,
        cluster.database,
        cluster.event_bus,
        owner.agent_id,
        RelaySupervision(),
        incarnation=owner,
        notes=handoff_notes(),
        host=quick,
    )
    ended = cluster.get(accepted["id"])
    assert ended["status"] == "rejected"
    assert ended["rejection_reason"] == "the controller relay is not running (no fresh heartbeat)"
    messages = (await graph.aget_state(config)).values["messages"]
    assert f"impersonation-start:{owner.agent_id}:0" in [m.id for m in messages]


async def test_settlement_activates_only_after_the_relay_is_ready(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="claude")
    cluster.heartbeat(accepted["id"], seconds_ago=0.0)
    graph, config = await idle_graph(owner.agent_id)
    assert await impersonation.settle_checkpoint(
        graph,
        cluster.database,
        cluster.event_bus,
        owner.agent_id,
        RelaySupervision(),
        incarnation=owner,
        notes=handoff_notes(),
        host=takeover_host(tmp_path, cluster.machine),
    )
    assert cluster.get(accepted["id"])["status"] == "active"
    messages = (await graph.aget_state(config)).values["messages"]
    assert [m.id for m in messages[-2:]] == [
        "impersonation-introduction",
        f"impersonation-start:{owner.agent_id}:0",
    ]


async def test_settlement_without_activation_leaves_an_accepted_lease_alone(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="claude")
    cluster.heartbeat(accepted["id"], seconds_ago=0.0)
    graph, config = await idle_graph(owner.agent_id)
    assert not await impersonation.settle_checkpoint(
        graph,
        cluster.database,
        cluster.event_bus,
        owner.agent_id,
        RelaySupervision(),
        activate_accepted=False,
        incarnation=owner,
        notes=handoff_notes(),
        host=takeover_host(tmp_path, cluster.machine),
    )
    assert cluster.get(accepted["id"])["status"] == "accepted"
    assert [m.id for m in (await graph.aget_state(config)).values["messages"]] == [
        "initial-request"
    ]


async def test_settlement_does_nothing_for_an_agent_without_a_lease_or_with_only_a_request(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    graph, _config = await idle_graph(owner.agent_id)

    async def settle() -> bool:
        return await impersonation.settle_checkpoint(
            graph,
            cluster.database,
            cluster.event_bus,
            owner.agent_id,
            RelaySupervision(),
            incarnation=owner,
            notes=handoff_notes(),
            host=takeover_host(tmp_path, cluster.machine),
        )

    assert not await settle()
    requested = cluster.lease(owner, provider="claude", phase="requested")
    assert not await settle()
    assert cluster.get(requested["id"])["status"] == "requested"


async def test_successor_admission_ends_a_stale_accepted_lease(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    from agent.ownership.hosted import admit_hosted_runtime
    from base.agents import impersonation as leases
    from base.agents.impersonation.tests.impersonation_support import recorded_tree
    from base.agents.tests.units import spawn_agent
    from base.api_contracts.caller_identity import CallerIdentity

    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    first = await admit_hosted_runtime(
        aops_pool, agent_id, machine_identity.name(), uuid4(), db=database
    )
    assert first is not None
    lease = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=3600,
        reason="Handle the next message",
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        authority=config_authority,
        local_machine=machine_identity.name(),
        name="Test name",
        executor_name="Test executor_name",
    )
    leases.prepare(
        database,
        event_bus,
        lease["id"],
        agent_id,
        first,
        "Handoff brief",
        local_machine=machine_identity.name(),
    )
    successor = await admit_hosted_runtime(
        aops_pool, agent_id, machine_identity.name(), uuid4(), db=database
    )
    assert successor is not None
    assert db_conn.execute(
        "SELECT status,ended_at IS NOT NULL,rejection_reason FROM agent_impersonations WHERE id=%s",
        (lease["id"],),
    ).fetchone() == ("expired", True, "aborted: native runtime restarted")
    assert db_conn.execute(
        "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (successor.generation, successor.owner)
