from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.proc import run_bounded
from ops.cluster import rpc as cluster_rpc
from ops.rpc_schemas import OpKind
from ops.tests.rpc_support import RPC_TEST_CREDENTIAL, local_ops_server, rpc_harness


def _db(*, database_gate: ProcessDbGate) -> Database:
    return Database.from_settings(gate=database_gate)


@pytest.mark.asyncio
async def test_completed_returns_result(*, database_gate: ProcessDbGate) -> None:
    captured = rpc_harness(
        lambda _r: httpx.Response(200, json={"status": "completed", "result": {"id": 5}})
    )
    result = await captured.rpc.dispatch_to_url(
        "runner-a",
        "spawn-launch",
        {"agent_id": 5, "launch_attempt_id": "00000000-0000-0000-0000-000000000001"},
        retries=0,
        ops_url="http://host:8106",
    )
    assert result == {"id": 5}
    req = captured.request
    assert str(req.url) == "http://host:8106/ops"
    import json

    body = json.loads(req.content)
    assert body["kind"] == "spawn-launch"
    assert body["payload"] == {
        "agent_id": 5,
        "launch_attempt_id": "00000000-0000-0000-0000-000000000001",
    }
    assert body["idempotency_key"].startswith("spawn-launch:")


@pytest.mark.asyncio
async def test_failed_raises_cluster_op_failed(*, database_gate: ProcessDbGate) -> None:
    captured = rpc_harness(
        lambda _r: httpx.Response(200, json={"status": "failed", "result": {"error": "boom"}})
    )
    with pytest.raises(cluster_rpc.ClusterOpFailed) as ei:
        await captured.rpc.dispatch_to_url(
            "runner-a", "lifecycle", {}, retries=0, ops_url="http://host:8106"
        )
    assert ei.value.result == {"error": "boom"}


@pytest.mark.asyncio
async def test_unknown_status_raises_unreachable(*, database_gate: ProcessDbGate) -> None:
    captured = rpc_harness(lambda _r: httpx.Response(200, json={"status": "pending", "result": {}}))
    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="malformed response"):
        await captured.rpc.dispatch_to_url(
            "runner-a", "status_probe", {}, retries=0, ops_url="http://host:8106"
        )


@pytest.mark.asyncio
async def test_non_200_raises_unreachable(*, database_gate: ProcessDbGate) -> None:
    captured = rpc_harness(lambda _r: httpx.Response(503, text="ops down"))
    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="503"):
        await captured.rpc.dispatch_to_url(
            "runner-a", "status_probe", {}, retries=0, ops_url="http://host:8106"
        )


@pytest.mark.asyncio
async def test_httpx_error_raises_unreachable(*, database_gate: ProcessDbGate) -> None:

    def _boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    captured = rpc_harness(_boom)
    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="unreachable"):
        await captured.rpc.dispatch_to_url(
            "runner-a", "status_probe", {}, retries=0, ops_url="http://host:8106"
        )


@pytest.mark.asyncio
async def test_status_probe_unreachable_logs_debug(
    caplog: pytest.LogCaptureFixture, *, database_gate: ProcessDbGate
) -> None:

    def _boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    captured = rpc_harness(_boom)
    with (
        caplog.at_level(logging.DEBUG, logger="ops.cluster.rpc"),
        pytest.raises(cluster_rpc.ClusterOpUnreachable),
    ):
        await captured.rpc.dispatch_to_url(
            "runner-a", "status_probe", {}, retries=0, ops_url="http://host:8106"
        )
    recs = [r for r in caplog.records if "unreachable after" in r.getMessage()]
    assert recs and all(r.levelno == logging.DEBUG for r in recs)


@pytest.mark.asyncio
async def test_non_probe_unreachable_stays_warning(
    caplog: pytest.LogCaptureFixture, *, database_gate: ProcessDbGate
) -> None:

    def _boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    captured = rpc_harness(_boom)
    with (
        caplog.at_level(logging.DEBUG, logger="ops.cluster.rpc"),
        pytest.raises(cluster_rpc.ClusterOpUnreachable),
    ):
        await captured.rpc.dispatch_to_url(
            "runner-a", "spawn-launch", {}, retries=0, ops_url="http://host:8106"
        )
    recs = [r for r in caplog.records if "unreachable after" in r.getMessage()]
    assert recs and all(r.levelno == logging.WARNING for r in recs)


@pytest.mark.asyncio
async def test_unregistered_machine_raises_target_absent(
    registered_machines: None, *, database_gate: ProcessDbGate
) -> None:
    with pytest.raises(cluster_rpc.ClusterOpTargetAbsent) as excinfo:
        await cluster_rpc.dispatch_to_machine(
            _db(database_gate=database_gate), "ghost", "status_probe", {}
        )
    assert isinstance(excinfo.value, cluster_rpc.ClusterOpUnreachable)
    assert "absent from the machines registry" in str(excinfo.value)


@pytest.mark.asyncio
async def test_null_gateway_url_raises_unreachable(
    registered_machines: None, db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
) -> None:
    db_conn.execute("UPDATE machines SET gateway_url = NULL WHERE name = 'runner-a'")
    db_conn.commit()
    with pytest.raises(cluster_rpc.ClusterOpUnreachable) as excinfo:
        await cluster_rpc.dispatch_to_machine(
            _db(database_gate=database_gate), "runner-a", "status_probe", {}, retries=0
        )
    assert not isinstance(excinfo.value, cluster_rpc.ClusterOpTargetAbsent)
    assert "cannot resolve an address" in str(excinfo.value)


@pytest.mark.asyncio
async def test_provided_ops_url_bypasses_machines_lookup() -> None:
    captured = rpc_harness(
        lambda _r: httpx.Response(200, json={"status": "completed", "result": {"ok": 1}})
    )
    result = await captured.rpc.dispatch_to_url(
        "runner-a", "status_probe", {}, ops_url="http://direct:8106"
    )
    assert result == {"ok": 1}
    assert str(captured.request.url) == "http://direct:8106/ops"


def test_default_timeout_comes_from_settings(tmp_path: Path) -> None:
    defaults = _process_defaults(tmp_path)
    assert defaults["timeouts"] == [12.5, 12.5, 12.5]
    assert defaults["connect_timeouts"] == [10.0, 10.0, 10.0]
    assert defaults["authorization"] == ["Bearer rpc-test"] * 3


@pytest.mark.asyncio
async def test_client_ignores_system_proxy(*, database_gate: ProcessDbGate) -> None:
    captured = rpc_harness(
        lambda _r: httpx.Response(200, json={"status": "completed", "result": {}})
    )
    await captured.rpc.dispatch_to_url(
        "runner-a", "status_probe", {}, retries=0, ops_url="http://host:8106"
    )
    assert captured.clients.trust_env == [False]


@pytest.mark.asyncio
async def test_connect_timeout_capped_by_timeout_s(*, database_gate: ProcessDbGate) -> None:
    captured = rpc_harness(
        lambda _r: httpx.Response(200, json={"status": "completed", "result": {}})
    )
    await captured.rpc.dispatch_to_url(
        "runner-a", "status_probe", {}, timeout_s=3.0, ops_url="http://host:8106"
    )
    assert captured.clients.timeouts[0].connect == 3.0


@pytest.mark.asyncio
async def test_transient_failure_retries_then_succeeds(*, database_gate: ProcessDbGate) -> None:
    calls = {"n": 0}

    def _flaky(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadError("connection reset by peer")
        return httpx.Response(200, json={"status": "completed", "result": {"ok": 1}})

    captured = rpc_harness(_flaky)
    sleeps = captured.timers.sleeps
    result = await captured.rpc.dispatch_to_url(
        "runner-a", "status_probe", {}, retries=1, ops_url="http://host:8106"
    )
    assert result == {"ok": 1}
    assert calls["n"] == 2
    assert len(captured.requests) == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_two_connection_resets_exhaust_single_retry(*, database_gate: ProcessDbGate) -> None:

    def _reset(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset by peer")

    captured = rpc_harness(_reset)
    sleeps = captured.timers.sleeps
    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="after 2 attempt"):
        await captured.rpc.dispatch_to_url(
            "runner-a", "status_probe", {}, retries=1, ops_url="http://host:8106"
        )
    assert len(captured.requests) == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_retries_exhausted_raises_after_all_attempts(*, database_gate: ProcessDbGate) -> None:

    def _boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer")

    captured = rpc_harness(_boom)
    sleeps = captured.timers.sleeps
    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="after 3 attempt"):
        await captured.rpc.dispatch_to_url(
            "runner-a", "lifecycle", {}, retries=2, ops_url="http://host:8106"
        )
    assert len(captured.requests) == 3
    assert sleeps == [0.5, 1.0]


def test_retry_delay_is_bounded_and_jittered() -> None:
    def midpoint(low: float, high: float) -> float:
        assert (low, high) == (0.5, 1.5)
        return 1.0

    assert [
        cluster_rpc.retry_delay_s(attempt, uniform=midpoint) for attempt in (0, 1, 2, 3, 10)
    ] == [0.5, 1.0, 2.0, 4.0, 4.0]


@pytest.mark.asyncio
async def test_non_idempotent_kind_retries_with_stable_idempotency_key(
    *, database_gate: ProcessDbGate
) -> None:
    import json

    calls = {"n": 0}

    def _flaky(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("response lost after the op ran")
        return httpx.Response(200, json={"status": "completed", "result": {"id": 7}})

    captured = rpc_harness(_flaky)
    result = await captured.rpc.dispatch_to_url(
        "runner-a",
        "spawn-launch",
        {"agent_id": 5, "launch_attempt_id": "00000000-0000-0000-0000-000000000001"},
        retries=2,
        ops_url="http://host:8106",
    )
    assert result == {"id": 7}
    keys = [json.loads(req.content)["idempotency_key"] for req in captured.requests]
    assert len(keys) == 2
    assert keys[0] == keys[1]
    assert keys[0].startswith("spawn-launch:")


@pytest.mark.asyncio
async def test_caller_supplied_idempotency_key_rides_envelope(
    *, database_gate: ProcessDbGate
) -> None:
    import json

    captured = rpc_harness(
        lambda _r: httpx.Response(200, json={"status": "completed", "result": {}})
    )
    await captured.rpc.dispatch_to_url(
        "runner-a",
        "spawn-launch",
        {},
        idempotency_key="my-logical-op-1",
        ops_url="http://host:8106",
    )
    body = json.loads(captured.request.content)
    assert body["idempotency_key"] == "my-logical-op-1"


@pytest.mark.asyncio
async def test_spawn_launch_defaults_to_its_agent_idempotency_key(
    *, database_gate: ProcessDbGate
) -> None:
    import json

    captured = rpc_harness(
        lambda _r: httpx.Response(200, json={"status": "completed", "result": {"id": 42}})
    )
    for target, payload in (
        ("runner-a", {"agent_id": 42, "launch_attempt_id": "00000000-0000-0000-0000-000000000001"}),
        ("runner-a", {"launch_attempt_id": "00000000-0000-0000-0000-000000000001", "agent_id": 42}),
        ("linux", {"agent_id": 42, "launch_attempt_id": "00000000-0000-0000-0000-000000000001"}),
        ("runner-a", {"agent_id": 42, "launch_attempt_id": "00000000-0000-0000-0000-000000000002"}),
    ):
        result = await captured.rpc.dispatch_to_url(
            target, "spawn-launch", payload, retries=0, ops_url="http://host:8106"
        )
        assert result == {"id": 42}
    keys = [json.loads(request.content)["idempotency_key"] for request in captured.requests]
    assert keys[0] == keys[1]
    assert keys[0].startswith("spawn-launch:runner-a:42:")
    assert keys[2] != keys[0]
    assert keys[3] != keys[0]


def test_launch_key_is_stable_within_attempt_and_rotates_between_attempts() -> None:
    first = {"agent_id": 42, "launch_attempt_id": "00000000-0000-0000-0000-000000000001"}
    second = {"agent_id": 42, "launch_attempt_id": "00000000-0000-0000-0000-000000000002"}
    key = cluster_rpc.default_idempotency_key("runner-a", "spawn-launch", first)
    assert key == cluster_rpc.default_idempotency_key("runner-a", "spawn-launch", first)
    assert key != cluster_rpc.default_idempotency_key("runner-a", "spawn-launch", second)
    assert key.startswith("spawn-launch:runner-a:42:")


@pytest.mark.asyncio
async def test_only_spawn_launch_reuses_a_business_idempotency_key(
    *, database_gate: ProcessDbGate
) -> None:
    import json

    captured = rpc_harness(
        lambda _r: httpx.Response(200, json={"status": "completed", "result": {}})
    )
    operations: tuple[tuple[OpKind, dict[str, Any]], ...] = (
        ("lifecycle", {"trigger_inbound_id": 17, "action": "restart"}),
    )
    for kind, payload in operations:
        await captured.rpc.dispatch_to_url(
            "runner-a", kind, payload, retries=0, ops_url="http://host:8106"
        )
        await captured.rpc.dispatch_to_url(
            "runner-a", kind, payload, retries=0, ops_url="http://host:8106"
        )
    keys = [json.loads(request.content)["idempotency_key"] for request in captured.requests]
    assert keys[0] != keys[1]
    assert keys[0].startswith("lifecycle:")


@pytest.mark.asyncio
async def test_idempotent_kind_carries_no_key(*, database_gate: ProcessDbGate) -> None:
    import json

    captured = rpc_harness(
        lambda _r: httpx.Response(200, json={"status": "completed", "result": {}})
    )
    await captured.rpc.dispatch_to_url("runner-a", "status_probe", {}, ops_url="http://host:8106")
    body = json.loads(captured.request.content)
    assert body.get("idempotency_key") is None


@pytest.mark.asyncio
async def test_5xx_is_retried(*, database_gate: ProcessDbGate) -> None:
    calls = {"n": 0}

    def _flaky(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="ops server restarting")
        return httpx.Response(200, json={"status": "completed", "result": {"paused": True}})

    captured = rpc_harness(_flaky)
    result = await captured.rpc.dispatch_to_url(
        "runner-a", "status_probe", {}, retries=2, ops_url="http://host:8106"
    )
    assert result == {"paused": True}
    assert calls["n"] == 2
    assert len(captured.requests) == 2


@pytest.mark.asyncio
async def test_4xx_is_not_retried(*, database_gate: ProcessDbGate) -> None:
    captured = rpc_harness(lambda _r: httpx.Response(404, text="nope"))
    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="returned 404"):
        await captured.rpc.dispatch_to_url(
            "runner-a", "status_probe", {}, retries=3, ops_url="http://host:8106"
        )
    assert len(captured.requests) == 1


@pytest.mark.asyncio
async def test_cluster_op_failed_is_not_retried(*, database_gate: ProcessDbGate) -> None:
    captured = rpc_harness(
        lambda _r: httpx.Response(200, json={"status": "failed", "result": {"error": "boom"}})
    )
    with pytest.raises(cluster_rpc.ClusterOpFailed):
        await captured.rpc.dispatch_to_url(
            "runner-a", "spawn-launch", {}, retries=3, ops_url="http://host:8106"
        )
    assert len(captured.requests) == 1


@pytest.mark.asyncio
async def test_malformed_response_is_not_retried(*, database_gate: ProcessDbGate) -> None:
    captured = rpc_harness(lambda _r: httpx.Response(200, json={"status": "pending", "result": {}}))
    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="malformed response"):
        await captured.rpc.dispatch_to_url(
            "runner-a", "spawn-launch", {}, retries=3, ops_url="http://host:8106"
        )
    assert len(captured.requests) == 1


def test_retries_default_from_settings(tmp_path: Path) -> None:
    defaults = _process_defaults(tmp_path)
    assert defaults["attempts"] == 3
    assert defaults["sleeps"] == [0.5, 1.0]


@pytest.mark.asyncio
async def test_unknown_kind_is_rejected_before_machine_lookup(
    *, database_gate: ProcessDbGate
) -> None:
    with pytest.raises(ValueError, match="unknown op kind"):
        await cluster_rpc.dispatch_to_machine(
            _db(database_gate=database_gate),
            "runner-a",
            "no-such-op",
            {},
            idempotency_key="explicit-key",
        )


@pytest.fixture
def registered_machines(db_conn: psycopg.Connection) -> None:
    db_conn.execute(
        "INSERT INTO machines (name, gateway_url, role) VALUES "
        "('runner-a', 'http://host:8106', ARRAY['agent-runner']), "
        "('linux', 'http://host:8106', ARRAY['agent-runner'])"
    )
    db_conn.commit()


def _unexpected_request(_request: httpx.Request) -> httpx.Response:
    raise AssertionError("the operation must fail before HTTP")


def _process_defaults(home: Path, case: str = "defaults") -> dict[str, object]:
    if case == "defaults":
        (home / ".env").write_text(
            "MEIVA_CLUSTER_RPC_TIMEOUT_SECONDS=12.5\nMEIVA_CLUSTER_RPC_MAX_RETRIES=2\n",
            encoding="utf-8",
        )
    child = run_bounded(
        [sys.executable, "-I", str(Path(__file__).with_name("_rpc_defaults_child.py")), case],
        env={
            "PATH": os.environ["PATH"],
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_PROCESS_PROFILE": "gateway",
            "MEIVA_TIMEZONE": "UTC",
            **({"MEIVA_API_TOKEN": RPC_TEST_CREDENTIAL} if case == "defaults" else {}),
        },
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert child.returncode == 0, child.stderr
    return json.loads(child.stdout)


@pytest.mark.asyncio
async def test_machine_root_uses_the_registered_ops_url(
    registered_machines: None, db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
) -> None:
    with local_ops_server() as (url, requests):
        db_conn.execute("UPDATE machines SET gateway_url = %s WHERE name = 'runner-a'", (url,))
        db_conn.commit()
        result = await cluster_rpc.dispatch_to_machine(
            _db(database_gate=database_gate), "runner-a", "status_probe", {"probe": True}, retries=0
        )
    assert result == {"ok": 1}
    assert requests == [("/ops", {"kind": "status_probe", "payload": {"probe": True}})]


def test_each_attempt_reads_fresh_credentials_without_changing_the_key(tmp_path: Path) -> None:
    observed = _process_defaults(tmp_path, "credentials")
    assert observed["events"] == ["credentials:1", "post", "credentials:2", "post"]
    assert observed["authorization"] == ["Bearer rpc-first", "Bearer rpc-second"]
    keys = observed["keys"]
    assert isinstance(keys, list)
    key_values = cast(list[object], keys)
    assert len(key_values) == 2
    assert key_values[0] == key_values[1]
