from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.db import Database
from gateway.tests.auth.gateway_config import gateway_test_client, gateway_test_config
from gateway.tests.support.runner_ops.ops_stub import (
    OpsCall,
    OpsReply,
    completed,
    refused,
    register_machine,
    runner_ops,
)
from ops.rpc_schemas import FieldWriteResult, InventoryReadResult, InventoryWriteOpResult


def _read(plugins: dict[str, Any], mcp_servers: dict[str, Any], machine: str) -> OpsReply:
    result = InventoryReadResult(machine=machine, plugins=plugins, mcp_servers=mcp_servers)
    return completed(result.model_dump(mode="json"))


def _plugin(*, enabled: bool, description: str = "") -> dict[str, Any]:
    return {"enabled": enabled, "can_enable": None, "reason": None, "description": description}


def _mcp(
    *, enabled: bool, can_enable: bool | None, reason: str | None, description: str = ""
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "can_enable": can_enable,
        "reason": reason,
        "description": description,
    }


# meiva:note
# The heartbeat pass's last probe of `name`, as `machine_status_snapshot` holds it.
def _snapshot(conn: psycopg.Connection, name: str, *, failures: int, age_s: float = 5.0) -> None:
    conn.execute(
        "INSERT INTO machine_status_snapshot "
        "(machine_name, observed_at, reachable, consecutive_failures, status, status_at) "
        "VALUES (%s, %s, %s, %s, %s, NULL)",
        (name, datetime.now(UTC) - timedelta(seconds=age_s), failures == 0, failures, Jsonb(None)),
    )
    conn.commit()


REMOTE = "remote-host"


@pytest.fixture(autouse=True)
def only_the_machines_a_test_registers(db_conn: psycopg.Connection) -> None:
    """The gateway's own machine is registered as a runner for every gateway test; the
    aggregate here is over exactly the machines each test serves."""
    db_conn.execute("TRUNCATE machines")
    db_conn.commit()


def test_aggregate_collapse_across_two_machines(db_conn: psycopg.Connection) -> None:
    a = {"inventory_read": _read({"X": _plugin(enabled=True, description="plugin X")}, {}, "A")}
    b = {"inventory_read": _read({}, {}, "B")}
    with (
        runner_ops(db_conn, "A", a),
        runner_ops(db_conn, "B", b),
        gateway_test_client() as client,
    ):
        resp = client.get("/api/inventory")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["machines"] == ["A", "B"]
    assert body["unreachable"] == []
    x = {r["name"]: r for r in body["plugins"]}["X"]
    assert x["hosts"]["A"]["present"] is True
    assert x["hosts"]["A"]["enabled"] is True
    assert x["hosts"]["B"]["present"] is False


def test_aggregate_buckets_unreachable_machine(db_conn: psycopg.Connection) -> None:
    a = {"inventory_read": _read({"X": _plugin(enabled=True)}, {}, "A")}
    with (
        runner_ops(db_conn, "A", a),
        runner_ops(db_conn, "B", {"inventory_read": refused(404, "no ack")}),
        gateway_test_client() as client,
    ):
        resp = client.get("/api/inventory")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unreachable"] == ["B"]
    x = {r["name"]: r for r in body["plugins"]}["X"]
    assert "B" not in x["hosts"]
    assert set(x["hosts"]) == {"A"}


def test_aggregate_skips_stopped_and_known_down_hosts(db_conn: psycopg.Connection) -> None:
    def answer(name: str) -> dict[str, OpsReply]:
        return {"inventory_read": _read({"X": _plugin(enabled=True)}, {}, name)}

    with (
        runner_ops(db_conn, "A", answer("A")) as a,
        runner_ops(db_conn, "B", answer("B")) as b,
        runner_ops(db_conn, "C", answer("C")) as c,
        runner_ops(db_conn, "D", answer("D")) as d,
        runner_ops(db_conn, "E", answer("E")) as e,
    ):
        db_conn.execute("UPDATE machines SET stopped_at = now() WHERE name = 'B'")
        db_conn.commit()
        # meiva:note
        # fresh, failed twice: known down
        _snapshot(db_conn, "C", failures=2)
        # meiva:note
        # fresh, failed once: still dialed
        _snapshot(db_conn, "D", failures=1)
        # meiva:note
        # stale: no evidence, dialed
        _snapshot(db_conn, "E", failures=5, age_s=3600.0)
        with gateway_test_client() as client:
            resp = client.get("/api/inventory")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r.name for r in (a, b, c, d, e) if r.calls_of("inventory_read")] == ["A", "D", "E"]
    assert body["machines"] == ["A", "B", "C", "D", "E"]
    assert body["unreachable"] == ["B", "C"]


# meiva:note
# A host that never answers is reported unreachable once the configured aggregate budget
# is spent, and the hosts that answered still make the matrix.
def test_aggregate_dials_run_under_the_aggregate_budget(db_conn: psycopg.Connection) -> None:
    release = threading.Event()

    def never_answers(call: OpsCall) -> OpsReply:
        del call
        release.wait(timeout=5.0)
        return completed({})

    a = {"inventory_read": _read({"X": _plugin(enabled=True)}, {}, "A")}
    with (
        runner_ops(db_conn, "A", a),
        runner_ops(db_conn, "B", {"inventory_read": never_answers}),
        gateway_test_client(config=gateway_test_config(inventory_host_wait_seconds=0.3)) as client,
    ):
        try:
            resp = client.get("/api/inventory")
        finally:
            release.set()
    assert resp.status_code == 200, resp.text
    assert resp.json()["unreachable"] == ["B"]
    assert [row["name"] for row in resp.json()["plugins"]] == ["X"]


def test_get_single_machine_view(db_conn: psycopg.Connection) -> None:
    answer = {
        "inventory_read": _read(
            {"b_plug": _plugin(enabled=True), "a_plug": _plugin(enabled=False)},
            {"srv": _mcp(enabled=True, can_enable=True, reason=None)},
            REMOTE,
        )
    }
    with runner_ops(db_conn, REMOTE, answer), gateway_test_client() as client:
        resp = client.get(f"/api/inventory?machine={REMOTE}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["machine"] == REMOTE
    assert [p["name"] for p in body["plugins"]] == ["a_plug", "b_plug"]
    assert body["plugins"][0]["kind"] == "plugin"
    assert body["mcp_servers"][0]["name"] == "srv"
    assert body["mcp_servers"][0]["kind"] == "mcp"


def test_get_unknown_machine_404() -> None:
    with gateway_test_client() as client:
        resp = client.get("/api/inventory?machine=ghost-host")
    assert resp.status_code == 404, resp.text
    assert "unknown agent-runner" in resp.json()["detail"]


def test_get_gateway_machine_404(db_conn: psycopg.Connection) -> None:
    register_machine(db_conn, "gw-host", roles=("gateway",))
    with gateway_test_client() as client:
        resp = client.get("/api/inventory?machine=gw-host")
    assert resp.status_code == 404, resp.text
    assert "unknown agent-runner" in resp.json()["detail"]


def test_aggregate_excludes_gateway_column(db_conn: psycopg.Connection) -> None:
    answer = {"inventory_read": _read({"X": _plugin(enabled=True)}, {}, REMOTE)}
    with (
        runner_ops(db_conn, REMOTE, answer),
        runner_ops(db_conn, "gw-host", answer, roles=("gateway",)) as gateway_host,
        gateway_test_client() as client,
    ):
        resp = client.get("/api/inventory")
    assert resp.status_code == 200, resp.text
    assert resp.json()["machines"] == [REMOTE]
    assert gateway_host.calls == []


def test_get_single_machine_offline_503(db_conn: psycopg.Connection) -> None:
    with (
        runner_ops(db_conn, REMOTE, {"inventory_read": refused(404, "no ack")}),
        gateway_test_client() as client,
    ):
        resp = client.get(f"/api/inventory?machine={REMOTE}")
    assert resp.status_code == 503, resp.text
    assert REMOTE in resp.json()["detail"]


def test_put_capability_rejection_surfaced(db_conn: psycopg.Connection) -> None:
    rejection = InventoryWriteOpResult(
        machine=REMOTE,
        plugin_results={"no_such": FieldWriteResult(ok=False, reason="not installed on this host")},
        mcp_results={},
        applied=False,
    )
    answer = {"inventory_write": completed(rejection.model_dump(mode="json"))}
    with runner_ops(db_conn, REMOTE, answer) as runner, gateway_test_client() as client:
        resp = client.put(
            f"/api/inventory?machine={REMOTE}",
            json={"plugins": {"no_such": True}, "mcp_servers": {}},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert body["plugin_results"]["no_such"]["ok"] is False
    assert body["plugin_results"]["no_such"]["reason"]
    assert runner.payloads("inventory_write") == [{"plugins": {"no_such": True}, "mcp_servers": {}}]


def test_put_unknown_machine_404() -> None:
    with gateway_test_client() as client:
        resp = client.put("/api/inventory?machine=ghost-host", json={"plugins": {"x": True}})
    assert resp.status_code == 404, resp.text
    assert "unknown agent-runner" in resp.json()["detail"]


def test_put_gateway_machine_404(db_conn: psycopg.Connection) -> None:
    register_machine(db_conn, "gw-host", roles=("gateway",))
    with gateway_test_client() as client:
        resp = client.put("/api/inventory?machine=gw-host", json={"plugins": {"x": True}})
    assert resp.status_code == 404, resp.text
    assert "unknown agent-runner" in resp.json()["detail"]


def test_put_missing_machine_400() -> None:
    with gateway_test_client() as client:
        resp = client.put("/api/inventory", json={"plugins": {"x": True}})
    assert resp.status_code == 400, resp.text
    assert "require ?machine=" in resp.json()["detail"]


def test_put_non_dict_half_422(db_conn: psycopg.Connection) -> None:
    register_machine(db_conn, REMOTE)
    with gateway_test_client() as client:
        resp = client.put(
            f"/api/inventory?machine={REMOTE}", json={"plugins": ["not", "a", "dict"]}
        )
    assert resp.status_code == 422, resp.text


# meiva:note
# The inventory routes read the registry through `request.app.state.db_pool`: swap in
# another pool and it is the one that serves the requests.
def test_inventory_borrows_from_the_apps_own_pool(
    db_conn: psycopg.Connection, database: Database
) -> None:
    written = InventoryWriteOpResult(
        machine=REMOTE,
        plugin_results={"X": FieldWriteResult(ok=True)},
        mcp_results={},
        applied=True,
    )
    answers = {
        "inventory_read": _read({"X": _plugin(enabled=True)}, {}, REMOTE),
        "inventory_write": completed(written.model_dump(mode="json")),
    }
    with (
        runner_ops(db_conn, REMOTE, answers),
        gateway_test_client() as client,
        database.pool(max_size=2) as other_pool,
    ):
        original_pool = client.app.state.db_pool
        original_borrowed = original_pool.get_stats().get("requests_num", 0)
        client.app.state.db_pool = other_pool
        borrowed = other_pool.get_stats().get("requests_num", 0)
        for url in ["/api/inventory", f"/api/inventory?machine={REMOTE}"]:
            response = client.get(url)
            assert response.status_code == 200, response.text
        response = client.put(f"/api/inventory?machine={REMOTE}", json={"plugins": {"X": False}})
        assert response.status_code == 200, response.text
        assert response.json()["applied"] is True
        assert other_pool.get_stats()["requests_num"] - borrowed == 4
        assert original_pool.get_stats().get("requests_num", 0) == original_borrowed
