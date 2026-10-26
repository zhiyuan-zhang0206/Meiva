import subprocess
import time
from pathlib import Path
from typing import Any, Literal

import pytest

from agent import impersonation
from agent.nodes import END
from agent.state import BaseAgentState
from agent.takeover.relay import STALE_RELAY_REASON
from agent.tests.impersonation.lease_support import (
    Cluster,
    exited_child,
    fixed_clock,
    native_context,
)
from agent.tests.impersonation.lease_support import cluster as cluster
from agent.tests.impersonation.lease_support import sleeper as sleeper
from base.agents import impersonation as leases
from base.agents.impersonation.tests.test_host_transport import FakeAppServer
from base.agents.impersonation.tests.test_host_transport import short_socket as short_socket
from base.agents.observation.relay_supervision import RelayChild, RelaySupervision
from base.native_process.runtime_incarnation import RuntimeIncarnation

DEGRADED = STALE_RELAY_REASON


async def _supervise(
    cluster: Cluster,
    session: dict[str, Any] | None,
    owner: RuntimeIncarnation,
    relays: RelaySupervision,
    *,
    native: bool = True,
) -> None:
    await impersonation.supervise_relay(
        cluster.database,
        cluster.event_bus,
        session,
        owner.agent_id,
        relays,
        incarnation=owner if native else None,
        clock=fixed_clock(),
    )


def _generation(cluster: Cluster, lease_id: str) -> int:
    return int(cluster.get(lease_id)["relay_generation"])


@pytest.mark.parametrize("same_lease", [False, True], ids=["other-lease", "replaced-transport"])
async def test_a_delayed_snapshot_cannot_retire_a_replacement_sender(
    cluster: Cluster, sleeper: subprocess.Popen[bytes], same_lease: bool
) -> None:
    owner = cluster.native_agent()
    active = cluster.lease(owner, phase="active")
    session = cluster.status(owner)
    assert session is not None
    relays = RelaySupervision()
    replacement = RelayChild(
        active["id"] if same_lease else "00000000-0000-0000-0000-000000000002",
        sleeper,
        "new-token",
        0.0,
        generation=session["relay_generation"] + 1,
    )
    relays.children[owner.agent_id] = replacement
    await _supervise(cluster, session, owner, relays)
    assert relays.children[owner.agent_id] is replacement
    assert sleeper.poll() is None
    assert cluster.get(active["id"])["relay_degraded_reason"] is None


async def test_a_missing_snapshot_cannot_retire_the_child_of_a_lease_that_is_still_open(
    cluster: Cluster, sleeper: subprocess.Popen[bytes]
) -> None:
    owner = cluster.native_agent()
    active = cluster.lease(owner, phase="active")
    relays = RelaySupervision()
    child = RelayChild(active["id"], sleeper, "token", 0.0, generation=0)
    relays.children[owner.agent_id] = child
    await _supervise(cluster, None, owner, relays)
    assert relays.children[owner.agent_id] is child
    assert sleeper.poll() is None


async def test_the_child_of_a_lease_the_database_shows_ended_is_stopped_and_retired(
    cluster: Cluster, sleeper: subprocess.Popen[bytes]
) -> None:
    owner = cluster.native_agent()
    active = cluster.lease(owner, phase="active")
    leases.release(
        cluster.database,
        cluster.event_bus,
        active["id"],
        "Done",
        local_machine=cluster.local_machine,
    )
    relays = RelaySupervision()
    relays.children[owner.agent_id] = RelayChild(active["id"], sleeper, "token", 0.0, generation=0)
    await _supervise(cluster, None, owner, relays)
    assert owner.agent_id not in relays.children
    assert sleeper.poll() is not None


async def test_a_stale_relay_is_recorded_as_degraded_and_never_replaced(
    cluster: Cluster,
) -> None:
    owner = cluster.native_agent()
    active = cluster.lease(owner, phase="active")
    cluster.heartbeat(active["id"], seconds_ago=300.0)
    session = cluster.status(owner)
    assert session is not None
    relays = RelaySupervision()
    await _supervise(cluster, session, owner, relays)
    after = cluster.get(active["id"])
    assert after["status"] == "active"
    assert after["relay_degraded_reason"] == DEGRADED
    assert after["relay_last_failure_at"] is not None
    assert after["relay_generation"] == active["relay_generation"]
    assert after["expires_at"] == active["expires_at"]
    assert not relays.children


async def test_a_stale_relay_with_a_live_child_keeps_that_child(
    cluster: Cluster, sleeper: subprocess.Popen[bytes]
) -> None:
    owner = cluster.native_agent()
    active = cluster.lease(owner, phase="active")
    cluster.heartbeat(active["id"], seconds_ago=300.0)
    session = cluster.status(owner)
    assert session is not None
    relays = RelaySupervision()
    child = RelayChild(active["id"], sleeper, "token", 0.0, generation=0)
    relays.children[owner.agent_id] = child
    await _supervise(cluster, session, owner, relays)
    assert cluster.get(active["id"])["relay_degraded_reason"] == DEGRADED
    assert relays.children[owner.agent_id] is child
    assert sleeper.poll() is None


async def test_a_freshly_spawned_child_gets_its_startup_window(
    cluster: Cluster, sleeper: subprocess.Popen[bytes]
) -> None:
    owner = cluster.native_agent()
    active = cluster.lease(owner, phase="active")
    session = cluster.status(owner)
    assert session is not None
    relays = RelaySupervision()
    relays.children[owner.agent_id] = RelayChild(
        active["id"], sleeper, "token", time.monotonic(), generation=0
    )
    await _supervise(cluster, session, owner, relays)
    assert cluster.get(active["id"])["relay_degraded_reason"] is None
    assert relays.children[owner.agent_id].process is sleeper


async def test_a_freshly_minted_credential_gets_its_startup_window(cluster: Cluster) -> None:
    owner = cluster.native_agent()
    active = cluster.lease(owner, phase="active")
    assert (
        leases.provision_relay(cluster.database, active["id"], owner, "fresh-credential")
        is not None
    )
    session = cluster.status(owner)
    assert session is not None
    await _supervise(cluster, session, owner, RelaySupervision())
    assert cluster.get(active["id"])["relay_degraded_reason"] is None


async def test_a_session_relay_with_a_stale_heartbeat_keeps_authority_and_names_unsupported_recovery(
    cluster: Cluster,
) -> None:
    owner = cluster.native_agent()
    active = cluster.lease(owner, provider="claude", phase="active")
    session = cluster.status(owner)
    assert session is not None
    await _supervise(cluster, session, owner, RelaySupervision())
    after = cluster.get(active["id"])
    assert after["status"] == "active"
    assert "unsupported" in after["relay_degraded_reason"]


async def test_a_fresh_heartbeat_records_no_degradation(cluster: Cluster) -> None:
    owner = cluster.native_agent()
    active = cluster.lease(owner, provider="claude", phase="active")
    cluster.heartbeat(active["id"], seconds_ago=0.0)
    session = cluster.status(owner)
    assert session is not None
    await _supervise(cluster, session, owner, RelaySupervision())
    assert cluster.get(active["id"])["relay_degraded_reason"] is None


async def test_a_host_without_the_native_incarnation_records_nothing(cluster: Cluster) -> None:
    owner = cluster.native_agent()
    active = cluster.lease(owner, provider="claude", phase="active")
    session = cluster.status(owner)
    assert session is not None
    await _supervise(cluster, session, owner, RelaySupervision(), native=False)
    assert cluster.get(active["id"])["relay_degraded_reason"] is None


async def test_the_claim_gate_supervises_the_relay_before_it_parks_the_graph(
    cluster: Cluster,
) -> None:
    owner = cluster.native_agent()
    active = cluster.lease(owner, phase="active")
    cluster.heartbeat(active["id"], seconds_ago=300.0)
    decision = await impersonation.claim_gate(
        BaseAgentState(), owner.agent_id, native_context(cluster, owner)
    )
    assert decision is not None and decision.goto == END
    assert cluster.get(active["id"])["relay_degraded_reason"] == DEGRADED


@pytest.mark.parametrize("heartbeat_age_s", [0.0, 300.0])
async def test_a_dead_relay_child_ends_the_lease_whatever_the_heartbeat(
    cluster: Cluster, tmp_path: Path, heartbeat_age_s: float
) -> None:
    owner = cluster.native_agent()
    active = cluster.lease(owner, phase="active")
    assert leases.provision_relay(cluster.database, active["id"], owner, "old-private") is not None
    cluster.heartbeat(active["id"], seconds_ago=heartbeat_age_s)
    session = cluster.status(owner)
    assert session is not None
    relays = RelaySupervision()
    relays.children[owner.agent_id] = RelayChild(
        active["id"],
        exited_child(tmp_path),
        "old-private",
        time.monotonic(),
        session["relay_generation"],
    )
    await _supervise(cluster, session, owner, relays)
    after = cluster.get(active["id"])
    assert after["status"] == "expired"
    assert after["ended_at"] is not None
    assert after["rejection_reason"] == "aborted: the bound relay process exited (exit code 0)"
    assert after["relay_degraded_reason"] is None
    assert after["relay_generation"] == session["relay_generation"]
    assert owner.agent_id not in relays.children


async def test_a_stale_relay_heartbeat_alone_keeps_the_lease(cluster: Cluster) -> None:
    owner = cluster.native_agent()
    active = cluster.lease(owner, phase="active")
    assert leases.provision_relay(cluster.database, active["id"], owner, "old-private") is not None
    cluster.age_relay(active["id"], seconds=600.0)
    session = cluster.status(owner)
    assert session is not None
    await _supervise(cluster, session, owner, RelaySupervision())
    after = cluster.get(active["id"])
    assert after["status"] == "active"
    assert after["relay_degraded_reason"] == DEGRADED
    assert after["expires_at"] == session["expires_at"]


def _assert_terminal_notice_failure(
    records: list[dict[str, Any]],
    delivery: Literal["accepted", "timeout", "refused"],
    lease_id: str,
    agent_id: int,
) -> None:
    failures = [
        record["extra"]
        for record in records
        if record["extra"].get("event") == "impersonation_terminal_notice_failed"
    ]
    if delivery == "accepted":
        assert failures == []
        return
    assert len(failures) == 1
    if delivery == "timeout":
        assert failures[0]["error"].startswith("TimeoutError:")
    else:
        assert failures[0]["error"] == (
            "turn/start refused (-32603: failed to submit turn input: "
            "ActiveTurnNotSteerable { turn_kind: Review })"
        )
    assert (failures[0]["agent_id"], failures[0]["lease_id"]) == (agent_id, lease_id)


@pytest.mark.parametrize("delivery", ["accepted", "timeout", "refused"])
async def test_ended_lease_notice_is_submitted_once_and_a_failure_is_reported_not_retried(
    cluster: Cluster,
    tmp_path: Path,
    loguru_records: list[dict[str, Any]],
    short_socket: Path,
    delivery: Literal["accepted", "timeout", "refused"],
) -> None:
    owner = cluster.native_agent()
    active = cluster.lease(owner, phase="active", codex_remote=f"unix://{short_socket}")
    leases.release(
        cluster.database,
        cluster.event_bus,
        active["id"],
        "Done",
        local_machine=cluster.local_machine,
    )
    relays = RelaySupervision()
    relays.children[owner.agent_id] = RelayChild(
        active["id"], exited_child(tmp_path), "token", 0.0, generation=0
    )
    server = FakeAppServer(hold_reply=delivery == "timeout", refuse_turn=delivery == "refused")
    server.start(short_socket)
    try:
        for _ in range(3):
            await _supervise(cluster, None, owner, relays, native=False)
    finally:
        server.stop()
    turns = [message for message in server.received if message.get("method") == "turn/start"]
    assert len(turns) == 1
    assert turns[0]["params"]["threadId"] == active["relay_thread_id"]
    text = turns[0]["params"]["input"][0]["text"]
    assert f"Notice ID: impersonation-ended:{active['id']}." in text
    _assert_terminal_notice_failure(loguru_records, delivery, active["id"], owner.agent_id)
    assert owner.agent_id not in relays.children
