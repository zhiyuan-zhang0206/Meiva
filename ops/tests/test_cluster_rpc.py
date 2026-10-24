# meiva:note
# The cluster RPC client, driven against a real HTTP listener on loopback with the real `httpx`
# client (`rpc_support`): the transport errors it classifies are the ones the wire produces, and
# the retry loop runs on a manual clock that records each wait. What a status or a retry count
# means is decided from values in `ops.cluster.rpc_plans` and asserted in `test_cluster_rpc_plans`.

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import GatewayAccess
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.proc import run_bounded
from ops.cluster import rpc as cluster_rpc
from ops.cluster.rpc import RpcClient, RpcConfig
from ops.rpc_schemas import OpKind
from ops.tests.rpc_support import (
    RPC_TEST_CREDENTIAL,
    Received,
    completed,
    dropped,
    envelope,
    failed,
    http_reply,
    rpc_client,
    scripted_ops_server,
    unreachable_url,
)

_LAUNCH = {"agent_id": 5, "launch_attempt_id": "00000000-0000-0000-0000-000000000001"}


def _db(*, database_gate: ProcessDbGate) -> Database:
    return Database.from_settings(gate=database_gate)


def _keys(received: list[Received]) -> list[object]:
    return [request.body.get("idempotency_key") for request in received]


# meiva:note
# Each wait between attempts is the backoff for its attempt scaled by a jitter in [0.5, 1.5], so
# the recorded waits are bounded rather than fixed.
def _assert_waits_follow_the_backoff(timers: ManualTimers, backoffs: list[float]) -> None:
    assert len(timers.sleeps) == len(backoffs)
    for wait, backoff in zip(timers.sleeps, backoffs, strict=True):
        assert 0.5 * backoff <= wait <= 1.5 * backoff


@pytest.mark.asyncio
async def test_completed_returns_result_and_presents_the_credential() -> None:
    rpc, _ = rpc_client()
    with scripted_ops_server(completed({"id": 5})) as server:
        result = await rpc.dispatch_to_url(
            "runner-a", "spawn-launch", _LAUNCH, retries=0, ops_url=server.url
        )

    assert result == {"id": 5}
    (request,) = server.received
    assert request.path == "/ops"
    assert request.authorization == f"Bearer {RPC_TEST_CREDENTIAL}"
    assert request.body["kind"] == "spawn-launch"
    assert request.body["payload"] == _LAUNCH
    assert str(request.body["idempotency_key"]).startswith("spawn-launch:")


@pytest.mark.asyncio
async def test_failed_raises_cluster_op_failed() -> None:
    rpc, _ = rpc_client()
    with (
        scripted_ops_server(failed({"error": "boom"})) as server,
        pytest.raises(cluster_rpc.ClusterOpFailed) as failure,
    ):
        await rpc.dispatch_to_url("runner-a", "lifecycle", {}, retries=0, ops_url=server.url)

    assert failure.value.result == {"error": "boom"}


@pytest.mark.asyncio
async def test_unknown_status_raises_unreachable() -> None:
    rpc, _ = rpc_client()
    with (
        scripted_ops_server(envelope("pending", {})) as server,
        pytest.raises(cluster_rpc.ClusterOpUnreachable, match="malformed response"),
    ):
        await rpc.dispatch_to_url("runner-a", "status_probe", {}, retries=0, ops_url=server.url)


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_raises_unreachable() -> None:
    rpc, _ = rpc_client()
    with (
        scripted_ops_server(http_reply(200, "<html>not an envelope</html>")) as server,
        pytest.raises(cluster_rpc.ClusterOpUnreachable, match="malformed response"),
    ):
        await rpc.dispatch_to_url("runner-a", "status_probe", {}, retries=0, ops_url=server.url)


@pytest.mark.asyncio
async def test_a_server_error_with_no_retry_left_raises_unreachable() -> None:
    rpc, _ = rpc_client()
    with (
        scripted_ops_server(http_reply(503, "ops down")) as server,
        pytest.raises(cluster_rpc.ClusterOpUnreachable, match="503"),
    ):
        await rpc.dispatch_to_url("runner-a", "status_probe", {}, retries=0, ops_url=server.url)


@pytest.mark.asyncio
async def test_a_refused_connection_raises_unreachable() -> None:
    rpc, _ = rpc_client()
    with pytest.raises(cluster_rpc.ClusterOpUnreachable, match="unreachable"):
        await rpc.dispatch_to_url(
            "runner-a", "status_probe", {}, retries=0, ops_url=unreachable_url()
        )


@pytest.mark.asyncio
async def test_status_probe_unreachable_logs_debug(caplog: pytest.LogCaptureFixture) -> None:
    rpc, _ = rpc_client()
    with (
        caplog.at_level(logging.DEBUG, logger="ops.cluster.rpc"),
        pytest.raises(cluster_rpc.ClusterOpUnreachable),
    ):
        await rpc.dispatch_to_url(
            "runner-a", "status_probe", {}, retries=0, ops_url=unreachable_url()
        )

    records = [r for r in caplog.records if "unreachable after" in r.getMessage()]
    assert records and all(r.levelno == logging.DEBUG for r in records)


@pytest.mark.asyncio
async def test_non_probe_unreachable_stays_warning(caplog: pytest.LogCaptureFixture) -> None:
    rpc, _ = rpc_client()
    with (
        caplog.at_level(logging.DEBUG, logger="ops.cluster.rpc"),
        pytest.raises(cluster_rpc.ClusterOpUnreachable),
    ):
        await rpc.dispatch_to_url(
            "runner-a", "spawn-launch", {}, retries=0, ops_url=unreachable_url()
        )

    records = [r for r in caplog.records if "unreachable after" in r.getMessage()]
    assert records and all(r.levelno == logging.WARNING for r in records)


@pytest.mark.asyncio
async def test_unregistered_machine_raises_target_absent(
    registered_machines: None, *, database_gate: ProcessDbGate
) -> None:
    rpc, _ = rpc_client()
    with pytest.raises(cluster_rpc.ClusterOpTargetAbsent) as excinfo:
        await rpc.dispatch_to_machine(_db(database_gate=database_gate), "ghost", "status_probe", {})

    assert isinstance(excinfo.value, cluster_rpc.ClusterOpUnreachable)
    assert "absent from the machines registry" in str(excinfo.value)


@pytest.mark.asyncio
async def test_null_gateway_url_raises_unreachable(
    registered_machines: None, db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
) -> None:
    db_conn.execute("UPDATE machines SET gateway_url = NULL WHERE name = 'runner-a'")
    db_conn.commit()
    rpc, _ = rpc_client()

    with pytest.raises(cluster_rpc.ClusterOpUnreachable) as excinfo:
        await rpc.dispatch_to_machine(
            _db(database_gate=database_gate), "runner-a", "status_probe", {}, retries=0
        )

    assert not isinstance(excinfo.value, cluster_rpc.ClusterOpTargetAbsent)
    assert "cannot resolve an address" in str(excinfo.value)


@pytest.mark.asyncio
async def test_unknown_kind_is_rejected_before_machine_lookup(
    *, database_gate: ProcessDbGate
) -> None:
    rpc, _ = rpc_client()
    with pytest.raises(ValueError, match="unknown op kind"):
        await rpc.dispatch_to_machine(
            _db(database_gate=database_gate),
            "runner-a",
            "no-such-op",
            {},
            idempotency_key="explicit-key",
        )


@pytest.mark.asyncio
async def test_unknown_kind_is_rejected_before_any_request() -> None:
    rpc, _ = rpc_client()
    with (
        scripted_ops_server(completed({})) as server,
        pytest.raises(ValueError, match="unknown op kind"),
    ):
        await rpc.dispatch_to_url("runner-a", "no-such-op", {}, ops_url=server.url)

    assert server.received == []


@pytest.mark.asyncio
async def test_machine_root_uses_the_registered_ops_url(
    registered_machines: None, db_conn: psycopg.Connection, *, database_gate: ProcessDbGate
) -> None:
    rpc, _ = rpc_client()
    with scripted_ops_server(completed({"ok": 1})) as server:
        db_conn.execute(
            "UPDATE machines SET gateway_url = %s WHERE name = 'runner-a'", (server.url,)
        )
        db_conn.commit()
        result = await rpc.dispatch_to_machine(
            _db(database_gate=database_gate), "runner-a", "status_probe", {"probe": True}, retries=0
        )

    assert result == {"ok": 1}
    (request,) = server.received
    assert (request.path, request.body) == (
        "/ops",
        {"kind": "status_probe", "payload": {"probe": True}},
    )


@pytest.mark.asyncio
async def test_provided_ops_url_bypasses_machines_lookup() -> None:
    with scripted_ops_server(completed({"ok": 1})) as server:
        result = await cluster_rpc.dispatch_to_url(
            "runner-a", "status_probe", {}, ops_url=server.url, retries=0
        )

    assert result == {"ok": 1}
    assert [(r.path, r.body) for r in server.received] == [
        ("/ops", {"kind": "status_probe", "payload": {}})
    ]


@pytest.mark.asyncio
async def test_transient_failure_retries_then_succeeds() -> None:
    rpc, timers = rpc_client()
    with scripted_ops_server(dropped(), completed({"ok": 1})) as server:
        result = await rpc.dispatch_to_url(
            "runner-a", "status_probe", {}, retries=1, ops_url=server.url
        )

    assert result == {"ok": 1}
    assert len(server.received) == 2
    _assert_waits_follow_the_backoff(timers, [0.5])


@pytest.mark.asyncio
async def test_two_lost_responses_exhaust_a_single_retry() -> None:
    rpc, timers = rpc_client()
    with (
        scripted_ops_server(dropped()) as server,
        pytest.raises(cluster_rpc.ClusterOpUnreachable, match="after 2 attempt"),
    ):
        await rpc.dispatch_to_url("runner-a", "status_probe", {}, retries=1, ops_url=server.url)

    assert len(server.received) == 2
    _assert_waits_follow_the_backoff(timers, [0.5])


@pytest.mark.asyncio
async def test_retries_exhausted_raises_after_all_attempts() -> None:
    rpc, timers = rpc_client()
    with (
        scripted_ops_server(dropped()) as server,
        pytest.raises(cluster_rpc.ClusterOpUnreachable, match="after 3 attempt"),
    ):
        await rpc.dispatch_to_url("runner-a", "lifecycle", {}, retries=2, ops_url=server.url)

    assert len(server.received) == 3
    _assert_waits_follow_the_backoff(timers, [0.5, 1.0])


@pytest.mark.asyncio
async def test_the_configured_retry_count_applies_when_the_caller_names_none() -> None:
    rpc, timers = rpc_client(max_retries=2)
    with (
        scripted_ops_server(dropped()) as server,
        pytest.raises(cluster_rpc.ClusterOpUnreachable, match="after 3 attempt"),
    ):
        await rpc.dispatch_to_url("runner-a", "status_probe", {}, ops_url=server.url)

    assert len(server.received) == 3
    _assert_waits_follow_the_backoff(timers, [0.5, 1.0])


@pytest.mark.asyncio
async def test_a_5xx_is_retried() -> None:
    rpc, _ = rpc_client()
    with scripted_ops_server(
        http_reply(503, "ops server restarting"), completed({"paused": True})
    ) as server:
        result = await rpc.dispatch_to_url(
            "runner-a", "status_probe", {}, retries=2, ops_url=server.url
        )

    assert result == {"paused": True}
    assert len(server.received) == 2


@pytest.mark.asyncio
async def test_a_4xx_is_not_retried() -> None:
    rpc, timers = rpc_client()
    with (
        scripted_ops_server(http_reply(404, "nope")) as server,
        pytest.raises(cluster_rpc.ClusterOpUnreachable, match="returned 404"),
    ):
        await rpc.dispatch_to_url("runner-a", "status_probe", {}, retries=3, ops_url=server.url)

    assert len(server.received) == 1
    assert timers.sleeps == []


@pytest.mark.asyncio
async def test_cluster_op_failed_is_not_retried() -> None:
    rpc, _ = rpc_client()
    with (
        scripted_ops_server(failed({"error": "boom"})) as server,
        pytest.raises(cluster_rpc.ClusterOpFailed),
    ):
        await rpc.dispatch_to_url("runner-a", "spawn-launch", {}, retries=3, ops_url=server.url)

    assert len(server.received) == 1


@pytest.mark.asyncio
async def test_a_malformed_response_is_not_retried() -> None:
    rpc, _ = rpc_client()
    with (
        scripted_ops_server(envelope("pending", {})) as server,
        pytest.raises(cluster_rpc.ClusterOpUnreachable, match="malformed response"),
    ):
        await rpc.dispatch_to_url("runner-a", "spawn-launch", {}, retries=3, ops_url=server.url)

    assert len(server.received) == 1


@pytest.mark.asyncio
async def test_a_negative_retry_count_still_makes_one_attempt() -> None:
    rpc, timers = rpc_client()
    with (
        scripted_ops_server(dropped()) as server,
        pytest.raises(cluster_rpc.ClusterOpUnreachable, match="after 1 attempt"),
    ):
        await rpc.dispatch_to_url("runner-a", "status_probe", {}, retries=-3, ops_url=server.url)

    assert len(server.received) == 1
    assert timers.sleeps == []


@pytest.mark.asyncio
async def test_non_idempotent_kind_retries_with_a_stable_idempotency_key() -> None:
    rpc, _ = rpc_client()
    with scripted_ops_server(dropped(), completed({"id": 7})) as server:
        result = await rpc.dispatch_to_url(
            "runner-a", "spawn-launch", _LAUNCH, retries=2, ops_url=server.url
        )

    assert result == {"id": 7}
    keys = _keys(server.received)
    assert len(keys) == 2
    assert keys[0] == keys[1]
    assert str(keys[0]).startswith("spawn-launch:")


@pytest.mark.asyncio
async def test_each_attempt_reads_fresh_credentials_without_changing_the_key() -> None:
    secrets = iter(["rpc-first", "rpc-second"])

    def access() -> GatewayAccess:
        return GatewayAccess(
            gateway_url="http://gateway",
            cluster_secret=next(secrets),
            remote_plane=False,
            machine_token=None,
            launcher_profile=None,
            gateway_home=False,
        )

    timers = ManualTimers()
    rpc = RpcClient(
        config=RpcConfig(timeout_seconds=3.0, max_retries=1), access=access, timers=timers
    )
    with scripted_ops_server(dropped(), completed({})) as server:
        await rpc.dispatch_to_url("runner-a", "spawn-launch", {"agent_id": 5}, ops_url=server.url)

    assert [r.authorization for r in server.received] == ["Bearer rpc-first", "Bearer rpc-second"]
    keys = _keys(server.received)
    assert keys[0] == keys[1] and keys[0] is not None


@pytest.mark.asyncio
async def test_caller_supplied_idempotency_key_rides_the_envelope() -> None:
    rpc, _ = rpc_client()
    with scripted_ops_server(completed({})) as server:
        await rpc.dispatch_to_url(
            "runner-a", "spawn-launch", {}, idempotency_key="my-logical-op-1", ops_url=server.url
        )

    assert _keys(server.received) == ["my-logical-op-1"]


@pytest.mark.asyncio
async def test_spawn_launch_defaults_to_its_agent_idempotency_key() -> None:
    rpc, _ = rpc_client()
    second_attempt = "00000000-0000-0000-0000-000000000002"
    with scripted_ops_server(completed({"id": 42})) as server:
        for target, payload in (
            ("runner-a", {"agent_id": 42, "launch_attempt_id": _LAUNCH["launch_attempt_id"]}),
            ("runner-a", {"launch_attempt_id": _LAUNCH["launch_attempt_id"], "agent_id": 42}),
            ("linux", {"agent_id": 42, "launch_attempt_id": _LAUNCH["launch_attempt_id"]}),
            ("runner-a", {"agent_id": 42, "launch_attempt_id": second_attempt}),
        ):
            result = await rpc.dispatch_to_url(
                target, "spawn-launch", payload, retries=0, ops_url=server.url
            )
            assert result == {"id": 42}

    keys = [str(key) for key in _keys(server.received)]
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
async def test_only_spawn_launch_reuses_a_business_idempotency_key() -> None:
    rpc, _ = rpc_client()
    operations: tuple[tuple[OpKind, dict[str, Any]], ...] = (
        ("lifecycle", {"trigger_inbound_id": 17, "action": "restart"}),
    )
    with scripted_ops_server(completed({})) as server:
        for kind, payload in operations:
            await rpc.dispatch_to_url("runner-a", kind, payload, retries=0, ops_url=server.url)
            await rpc.dispatch_to_url("runner-a", kind, payload, retries=0, ops_url=server.url)

    keys = [str(key) for key in _keys(server.received)]
    assert keys[0] != keys[1]
    assert keys[0].startswith("lifecycle:")


@pytest.mark.asyncio
async def test_idempotent_kind_carries_no_key() -> None:
    rpc, _ = rpc_client()
    with scripted_ops_server(completed({})) as server:
        await rpc.dispatch_to_url("runner-a", "status_probe", {}, ops_url=server.url)

    assert "idempotency_key" not in server.received[0].body


@pytest.fixture
def registered_machines(db_conn: psycopg.Connection) -> None:
    db_conn.execute(
        "INSERT INTO machines (name, gateway_url, role) VALUES "
        "('runner-a', 'http://host:8106', ARRAY['agent-runner']), "
        "('linux', 'http://host:8106', ARRAY['agent-runner'])"
    )
    db_conn.commit()


# meiva:note
# The child process is a real gateway-profile process whose unit `.env` carries the RPC timeout
# and retry settings; `RpcClient.system()` is the one place those settings are read.
def _system_client_child(
    home: Path, case: str, ops_url: str, *, extra_env: dict[str, str] | None = None
) -> dict[str, object]:
    (home / ".env").write_text(
        "MEIVA_CLUSTER_RPC_TIMEOUT_SECONDS=12.5\nMEIVA_CLUSTER_RPC_MAX_RETRIES=2\n",
        encoding="utf-8",
    )
    child = run_bounded(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("_rpc_defaults_child.py")),
            case,
            ops_url,
        ],
        env={
            "PATH": os.environ["PATH"],
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_PROCESS_PROFILE": "gateway",
            "MEIVA_TIMEZONE": "UTC",
            "MEIVA_API_TOKEN": RPC_TEST_CREDENTIAL,
            **(extra_env or {}),
        },
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert child.returncode == 0, child.stderr
    return json.loads(child.stdout)


def test_the_system_client_reads_its_timeout_retries_and_credential_from_the_unit(
    tmp_path: Path,
) -> None:
    with scripted_ops_server(completed({})) as server:
        reported = _system_client_child(tmp_path, "config", server.url)

    assert reported == {
        "timeout_seconds": 12.5,
        "max_retries": 2,
        "authorization": f"Bearer {RPC_TEST_CREDENTIAL}",
    }
    assert [r.authorization for r in server.received] == [f"Bearer {RPC_TEST_CREDENTIAL}"]


# meiva:note
# Cluster peers are private-network addresses, so the client ignores the system proxy settings:
# with a dead proxy in the environment the dispatch still reaches the loopback server.
def test_the_client_ignores_the_system_proxy(tmp_path: Path) -> None:
    dead_proxy = "http://127.0.0.1:9"
    with scripted_ops_server(completed({"ok": 1})) as server:
        result = _system_client_child(
            tmp_path,
            "proxy",
            server.url,
            extra_env={
                "HTTP_PROXY": dead_proxy,
                "http_proxy": dead_proxy,
                "ALL_PROXY": dead_proxy,
                "all_proxy": dead_proxy,
            },
        )

    assert result == {"ok": 1}
    assert len(server.received) == 1
