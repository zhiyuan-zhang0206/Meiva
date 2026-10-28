# meiva:note
# The liveness round probes machines through the cluster client it was built with: the credential of
# the test client reaches a loopback ops server standing in for the machine's runner, which no
# client built from the process's own configuration would present. (The completion digest round
# hands its client on too, but a digest is a system notice and never wakes a terminated agent, so
# no digest ever reaches the client and there is nothing to observe.)

from __future__ import annotations

import asyncio

import psycopg
from psycopg_pool import ConnectionPool

from base.clock.tests.seeded_rng import seeded_rng
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from ops.cluster.rpc import RpcClient
from ops.tests.rpc_support import RPC_TEST_CREDENTIAL, completed, scripted_ops_server
from services.wake.heartbeat.daemon import AgentLivenessRound
from services.wake.heartbeat.tests.machine_probe_setup import pool as pool
from services.wake.heartbeat.tests.machine_probe_setup import register_machine

_STATUS: dict[str, object] = {
    "machine_name": "runner-1",
    "serve_gateway": False,
    "serve_agent_runner": True,
    "paused": False,
    "running_sha": "sha-1",
    "agent_host_online": True,
}


def test_the_liveness_round_probes_each_machine_through_its_own_cluster_client(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    rpc: RpcClient,
    *,
    database_gate: ProcessDbGate,
) -> None:
    with scripted_ops_server(completed(_STATUS)) as runner:
        register_machine(db_conn, "runner-1", gateway_url=runner.url)
        round_ = AgentLivenessRound(
            Database.from_settings(gate=database_gate),
            pool,
            EventBus.from_settings(rng=seeded_rng()),
            rpc,
        )

        asyncio.run(round_.run_round())

    assert [call.authorization for call in runner.received] == [f"Bearer {RPC_TEST_CREDENTIAL}"]
    row = db_conn.execute(
        "SELECT reachable FROM machine_status_snapshot WHERE machine_name = 'runner-1'"
    ).fetchone()
    assert row == (True,)
