import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Timer
from typing import Any
from uuid import uuid4

import pytest

from agent.impersonation import draw_relay_token, establish_relay
from agent.takeover.process import spawn_relay
from agent.takeover.relay import (
    CREDENTIAL_RENEWED_REASON,
    NOT_READY_REASON,
    SESSION_RELAY_DOWN_REASON,
    UNBOUND_REASON,
    RelayBinding,
    RelayWait,
    startup_failure,
)
from agent.tests.impersonation.lease_support import Cluster, relay_program, stub_host
from agent.tests.impersonation.lease_support import cluster as cluster
from base.agents import impersonation as leases
from base.agents.impersonation.host_transport import antigravity_endpoint_path
from base.agents.impersonation.relay import RelayAction
from base.agents.observation.relay_supervision import RelaySupervision, terminate_relay
from base.native_process.runtime_incarnation import RuntimeIncarnation

CREDENTIAL = "private-test-credential"
SLOW = RelayWait(timeout_s=20.0, poll_s=0.05)


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws; a seed names the draw a
# test expects.
def _seeded(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311


def _wait_for(path: Path, *, complete: bytes = b"") -> bytes:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if path.exists() and path.read_bytes().endswith(complete) and path.read_bytes():
            return path.read_bytes()
        time.sleep(0.02)
    raise AssertionError(f"{path.name} never appeared")


def _establish(
    cluster: Cluster,
    session: dict[str, Any],
    owner: RuntimeIncarnation,
    relays: RelaySupervision,
    *,
    tmp_path: Path,
    mode: str,
    wait: RelayWait = SLOW,
    seed: int = 0,
) -> bool:
    return establish_relay(
        cluster.database,
        cluster.event_bus,
        session,
        owner,
        relays,
        host=stub_host(cluster, tmp_path, mode, wait=wait, rng=_seeded(seed)),
    )


@pytest.mark.parametrize("provider", ["claude", "dsh"])
def test_a_session_relay_with_a_fresh_heartbeat_stands_without_a_transition(
    cluster: Cluster, tmp_path: Path, provider: str
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider=provider)
    cluster.heartbeat(accepted["id"], seconds_ago=0.0)
    session = cluster.status(owner)
    assert session is not None
    assert _establish(cluster, session, owner, RelaySupervision(), tmp_path=tmp_path, mode="exit:9")
    assert cluster.get(accepted["id"])["status"] == "accepted"


def test_a_session_relay_without_a_heartbeat_rolls_the_lease_back_when_the_wait_is_spent(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="claude")
    session = cluster.status(owner)
    assert session is not None
    assert not _establish(
        cluster,
        session,
        owner,
        RelaySupervision(),
        tmp_path=tmp_path,
        mode="exit:9",
        wait=RelayWait(timeout_s=0.2, poll_s=0.05),
    )
    ended = cluster.get(accepted["id"])
    assert ended["status"] == "rejected"
    assert ended["rejection_reason"] == SESSION_RELAY_DOWN_REASON


def test_a_session_relay_that_heartbeats_inside_the_wait_stands(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="dsh")
    session = cluster.status(owner)
    assert session is not None
    late = Timer(0.3, lambda: cluster.heartbeat(accepted["id"], seconds_ago=0.0))
    late.start()
    try:
        assert _establish(
            cluster, session, owner, RelaySupervision(), tmp_path=tmp_path, mode="exit:9"
        )
    finally:
        late.join()
    assert cluster.get(accepted["id"])["status"] == "accepted"


def test_a_lease_someone_else_already_ended_needs_no_relay_and_keeps_its_own_reason(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="claude")
    stale = cluster.status(owner)
    assert stale is not None
    leases.fail_acceptance(
        cluster.database, cluster.event_bus, accepted["id"], owner, "ended by the controller"
    )
    assert not _establish(
        cluster, stale, owner, RelaySupervision(), tmp_path=tmp_path, mode="exit:9"
    )
    assert cluster.get(accepted["id"])["rejection_reason"] == "ended by the controller"


def test_a_lease_without_a_relay_binding_is_rolled_back(cluster: Cluster, tmp_path: Path) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="claude")
    session = cluster.status(owner)
    assert session is not None
    unbound = {**session, "relay_provider": None}
    assert not _establish(
        cluster, unbound, owner, RelaySupervision(), tmp_path=tmp_path, mode="exit:9"
    )
    assert cluster.get(accepted["id"])["rejection_reason"] == UNBOUND_REASON


def test_a_codex_relay_is_provisioned_spawned_and_awaited_until_its_first_heartbeat(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="codex")
    session = cluster.status(owner)
    assert session is not None
    relays = RelaySupervision()
    marker = tmp_path / "relay"
    with ThreadPoolExecutor(max_workers=1) as pool:
        ready = pool.submit(
            _establish,
            cluster,
            session,
            owner,
            relays,
            tmp_path=tmp_path,
            mode="serve",
            seed=11,
        )
        try:
            token = _wait_for(marker / "token", complete=b"\n").decode().strip()
            leases.relay_heartbeat(
                cluster.database, accepted["id"], token, local_machine=cluster.local_machine
            )
            assert ready.result(timeout=20) is True
        finally:
            for child in relays.children.values():
                terminate_relay(child)
    child = relays.children[owner.agent_id]
    assert child.token == token
    assert token == draw_relay_token(_seeded(11))
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
    assert child.lease_id == accepted["id"]
    assert child.generation == cluster.get(accepted["id"])["relay_generation"] == 1
    assert json.loads((marker / "argv.json").read_text()) == [
        "impersonate",
        "relay",
        str(owner.agent_id),
        "--lease-id",
        accepted["id"],
        "--provider",
        "codex",
        "--thread-id",
        session["relay_thread_id"],
        "--token-stdin",
    ]
    assert cluster.get(accepted["id"])["status"] == "accepted"


def test_a_codex_relay_that_exits_during_startup_rolls_the_lease_back(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="codex")
    session = cluster.status(owner)
    assert session is not None
    relays = RelaySupervision()
    assert not _establish(cluster, session, owner, relays, tmp_path=tmp_path, mode="exit:5")
    ended = cluster.get(accepted["id"])
    assert ended["status"] == "rejected"
    assert ended["rejection_reason"] == startup_failure("codex", 5)
    assert "exit code 5" in ended["rejection_reason"]
    assert not relays.children


def test_a_codex_relay_that_never_heartbeats_is_stopped_and_the_lease_rolled_back(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="codex")
    session = cluster.status(owner)
    assert session is not None
    relays = RelaySupervision()
    assert not _establish(
        cluster,
        session,
        owner,
        relays,
        tmp_path=tmp_path,
        mode="serve",
        wait=RelayWait(timeout_s=0.5, poll_s=0.05),
    )
    ended = cluster.get(accepted["id"])
    assert ended["status"] == "rejected"
    assert ended["rejection_reason"] == NOT_READY_REASON
    assert not relays.children
    pid = int((tmp_path / "relay" / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_credential_replaced_during_startup_rolls_the_lease_back(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="codex")
    session = cluster.status(owner)
    assert session is not None
    relays = RelaySupervision()
    with ThreadPoolExecutor(max_workers=1) as pool:
        outcome = pool.submit(
            _establish, cluster, session, owner, relays, tmp_path=tmp_path, mode="serve"
        )
        try:
            _wait_for(tmp_path / "relay" / "token", complete=b"\n")
            cluster.db_conn.execute(
                "UPDATE agent_impersonations SET relay_token_hash='replaced' WHERE id=%s",
                (accepted["id"],),
            )
            cluster.db_conn.commit()
            assert outcome.result(timeout=20) is False
        finally:
            for child in relays.children.values():
                terminate_relay(child)
    assert cluster.get(accepted["id"])["rejection_reason"] == CREDENTIAL_RENEWED_REASON


def test_a_codex_lease_that_is_no_longer_native_held_spawns_no_relay(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="codex")
    stale = cluster.status(owner)
    assert stale is not None
    leases.fail_acceptance(
        cluster.database, cluster.event_bus, accepted["id"], owner, "ended by the controller"
    )
    relays = RelaySupervision()
    assert not _establish(cluster, stale, owner, relays, tmp_path=tmp_path, mode="serve")
    assert not relays.children
    assert not (tmp_path / "relay" / "pid").exists()


def test_an_unreadable_antigravity_record_rolls_back_with_its_path(
    cluster: Cluster, tmp_path: Path
) -> None:
    # meiva:note
    # A relay that dies at startup leaves only an exit code in the lease's reason, so
    # the accepting runtime resolves the destination first and names the record it
    # could not use. The assertion is the operator-visible rejection reason: resolving
    # before spawning is what puts the fact there, and no relay process exists to start.
    # meiva:link docs/conventions/agents/agent-impersonation-hosts.md
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="antigravity")
    session = cluster.status(owner)
    assert session is not None
    record = antigravity_endpoint_path(session["relay_thread_id"], home=tmp_path)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("not json", encoding="utf-8")
    relays = RelaySupervision()
    assert not _establish(cluster, session, owner, relays, tmp_path=tmp_path, mode="serve")
    ended = cluster.get(accepted["id"])
    assert ended["status"] == "rejected"
    assert "the Antigravity endpoint is unusable" in ended["rejection_reason"]
    assert str(record) in ended["rejection_reason"]
    assert not (tmp_path / "relay" / "pid").exists()


def test_the_spawned_relay_receives_its_credential_over_the_pipe_only(tmp_path: Path) -> None:
    marker = tmp_path / "received"
    marker.mkdir()
    binding = RelayBinding(
        agent_id=1,
        lease_id="lease",
        provider="codex",
        thread_id="thread",
        codex_remote=None,
        antigravity_endpoint_file=None,
        codex_home=None,
    )
    child = spawn_relay(
        relay_program(marker, "record"), binding, token=CREDENTIAL, environ=dict(os.environ)
    )
    child.wait(timeout=10)
    assert (marker / "token").read_bytes() == CREDENTIAL.encode() + b"\n"
    argv = json.loads((marker / "argv.json").read_text())
    assert argv[:2] == ["impersonate", "relay"]
    assert all(CREDENTIAL not in part for part in argv)


# meiva:note
# The relay's environment is projected from the environment the host hands in, not from the live
# one: the handed-in `MEIVA_HOME` (host-scope, forwarded) reaches the child, the process's own does not.
def test_the_spawned_relay_environment_is_projected_from_the_environment_it_is_given(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "received"
    marker.mkdir()
    binding = RelayBinding(
        agent_id=1,
        lease_id="lease",
        provider="codex",
        thread_id="thread",
        codex_remote=None,
        antigravity_endpoint_file=None,
        codex_home=None,
    )
    given_home = str(tmp_path / "given-home")

    child = spawn_relay(
        relay_program(marker, "record"),
        binding,
        token=CREDENTIAL,
        environ={**os.environ, "MEIVA_HOME": given_home},
    )
    child.wait(timeout=10)

    assert json.loads((marker / "env.json").read_text())["MEIVA_HOME"] == given_home


def _hand_over(cluster: Cluster, predecessor: RuntimeIncarnation) -> RuntimeIncarnation:
    successor = RuntimeIncarnation(predecessor.agent_id, uuid4(), uuid4())
    cluster.db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s WHERE id=%s",
        (successor.generation, successor.owner, successor.agent_id),
    )
    cluster.db_conn.execute(
        "UPDATE agent_impersonations SET accepted_generation=%s,accepted_owner=%s "
        "WHERE agent_id=%s AND status IN ('accepted','active')",
        (successor.generation, successor.owner, successor.agent_id),
    )
    cluster.db_conn.commit()
    return successor


def test_a_carried_takeover_adopts_the_predecessor_relay_without_spawning(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="codex")
    minted = leases.provision_relay(cluster.database, accepted["id"], owner, "predecessor")
    assert minted is not None
    cluster.heartbeat(accepted["id"], seconds_ago=0.0)
    successor = _hand_over(cluster, owner)
    session = cluster.status(successor)
    assert session is not None
    relays = RelaySupervision()
    assert _establish(cluster, session, successor, relays, tmp_path=tmp_path, mode="serve")
    assert not relays.children
    assert cluster.get(accepted["id"])["relay_generation"] == 1
    assert (
        leases.relay_get(
            cluster.database,
            cluster.event_bus,
            accepted["id"],
            "predecessor",
            local_machine=cluster.local_machine,
        )["status"]
        == "accepted"
    )
    assert cluster.get(accepted["id"])["status"] == "accepted"


def test_a_carried_takeover_remints_when_the_predecessor_relay_stopped_beating(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="codex")
    assert leases.provision_relay(cluster.database, accepted["id"], owner, "predecessor")
    cluster.heartbeat(accepted["id"], seconds_ago=600.0)
    successor = _hand_over(cluster, owner)
    session = cluster.status(successor)
    assert session is not None
    relays = RelaySupervision()
    marker = tmp_path / "relay"
    with ThreadPoolExecutor(max_workers=1) as pool:
        ready = pool.submit(
            _establish, cluster, session, successor, relays, tmp_path=tmp_path, mode="serve"
        )
        try:
            token = _wait_for(marker / "token", complete=b"\n").decode().strip()
            leases.relay_heartbeat(
                cluster.database, accepted["id"], token, local_machine=cluster.local_machine
            )
            assert ready.result(timeout=20) is True
        finally:
            for child in relays.children.values():
                terminate_relay(child)
    assert relays.children[successor.agent_id].generation == 2
    with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
        leases.relay_get(
            cluster.database,
            cluster.event_bus,
            accepted["id"],
            "predecessor",
            local_machine=cluster.local_machine,
        )


def test_a_carried_takeover_with_no_usable_carrier_rolls_back_instead_of_activating(
    cluster: Cluster, tmp_path: Path
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="codex")
    successor = _hand_over(cluster, owner)
    session = cluster.status(successor)
    assert session is not None
    relays = RelaySupervision()
    assert not _establish(
        cluster,
        session,
        successor,
        relays,
        tmp_path=tmp_path,
        mode="serve",
        wait=RelayWait(timeout_s=0.5, poll_s=0.05),
    )
    ended = cluster.get(accepted["id"])
    assert ended["status"] == "rejected"
    assert ended["rejection_reason"] == NOT_READY_REASON
    assert not relays.children


# meiva:note
# The provisioning criterion asks whether a minting pair is recorded, not whether
# `relay_generation` moved. The two questions disagree in exactly two states, and neither is one
# the production path can reach — minting always writes both — so each test builds its state by
# hand and pins the branch the criterion takes there: a lease with no recorded pair mints even
# when its generation is high, and a lease with a beating recorded pair is adopted even at
# generation zero.
def test_a_lease_with_no_recorded_mint_pair_mints_even_with_a_generation(cluster: Cluster) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="codex")
    cluster.db_conn.execute(
        "UPDATE agent_impersonations SET relay_generation=1,relay_minted_generation=NULL,"
        "relay_minted_owner=NULL WHERE id=%s",
        (accepted["id"],),
    )
    cluster.db_conn.commit()
    cluster.heartbeat(accepted["id"], seconds_ago=0.0)
    successor = _hand_over(cluster, owner)

    provision = leases.provision_relay(
        cluster.database, accepted["id"], successor, "successor-token"
    )

    assert provision is not None
    assert provision.action is RelayAction.MINTED
    assert cluster.get(accepted["id"])["relay_generation"] == 2


# meiva:note
# The other side of the same criterion: a recorded pair whose carrier still beats is adopted at
# generation zero, because `relay_generation` never decided it.
def test_a_lease_with_a_beating_recorded_carrier_is_adopted_at_generation_zero(
    cluster: Cluster,
) -> None:
    owner = cluster.native_agent()
    accepted = cluster.lease(owner, provider="codex")
    cluster.db_conn.execute(
        "UPDATE agent_impersonations SET relay_generation=0,relay_minted_generation=%s,"
        "relay_minted_owner=%s WHERE id=%s",
        (owner.generation, owner.owner, accepted["id"]),
    )
    cluster.db_conn.commit()
    cluster.heartbeat(accepted["id"], seconds_ago=0.0)
    successor = _hand_over(cluster, owner)

    provision = leases.provision_relay(
        cluster.database, accepted["id"], successor, "successor-token"
    )

    assert provision is not None
    assert provision.action is RelayAction.ADOPTED
    assert cluster.get(accepted["id"])["relay_generation"] == 0
