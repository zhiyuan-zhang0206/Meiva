# meiva:note
# `dispatch_to_machine` end to end: the real registry read, the real HTTP client and the real
# result handling, against a loopback listener registered as a runner in `machines`. The client is
# the one the test hands in, as every caller does.

from __future__ import annotations

import psycopg
import pytest

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from ops.cluster.rpc import ClusterOpFailed
from ops.tests.rpc_support import completed, failed, http_reply, rpc_client, scripted_ops_server


def _register(conn: psycopg.Connection, name: str, ops_url: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machines (name, role, gateway_url) VALUES (%s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET role = EXCLUDED.role, "
            "gateway_url = EXCLUDED.gateway_url",
            (name, ["agent-runner"], ops_url),
        )
    conn.commit()


async def test_dispatch_resolves_registered_url_and_round_trips(
    db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
) -> None:
    with scripted_ops_server(completed({"ran": True})) as server:
        _register(db_conn, "runner-x", server.url)

        result = await rpc_client()[0].dispatch_to_machine(
            Database.from_settings(gate=database_gate), "runner-x", "status_probe", {"foo": "bar"}
        )

    assert result == {"ran": True}
    assert [(r.path, r.body) for r in server.received] == [
        ("/ops", {"kind": "status_probe", "payload": {"foo": "bar"}})
    ]


async def test_dispatch_failed_status_raises_cluster_op_failed(
    db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
) -> None:
    with scripted_ops_server(failed({"error": "boom"})) as server:
        _register(db_conn, "runner-y", server.url)
        with pytest.raises(ClusterOpFailed) as failure:
            await rpc_client()[0].dispatch_to_machine(
                Database.from_settings(gate=database_gate), "runner-y", "status_probe", {}
            )

    assert failure.value.result == {"error": "boom"}


async def test_dispatch_retries_transient_503_over_real_wire(
    db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
) -> None:
    with scripted_ops_server(http_reply(503), completed({"ran": True})) as server:
        _register(db_conn, "runner-z", server.url)
        result = await rpc_client()[0].dispatch_to_machine(
            Database.from_settings(gate=database_gate),
            "runner-z",
            "status_probe",
            {"ping": 1},
            retries=2,
        )

    assert result == {"ran": True}
    assert [r.body for r in server.received] == [
        {"kind": "status_probe", "payload": {"ping": 1}}
    ] * 2
