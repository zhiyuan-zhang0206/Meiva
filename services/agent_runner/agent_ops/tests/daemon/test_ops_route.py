# meiva:note
# `ops_route`, the POST /ops handler: admission, the concurrency cap, keys and crashes.

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ops.rpc_schemas import LaunchAgentRequest
from services.agent_runner.agent_ops.tests.ops_support import (
    Ops,
    config_write,
    executions,
    held_env_lock,
)


async def _until(condition: Callable[[], bool], *, timeout_s: float = 10.0) -> None:
    for _ in range(int(timeout_s / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never held")


async def test_a_finished_operation_is_wrapped_in_the_status_and_result_envelope(
    ops: Ops,
) -> None:
    code, answer = await ops.route({"kind": "config_read", "payload": {}})

    assert code == 200
    assert answer["status"] == "completed"
    assert answer["result"]["machine"] == ops.machine.name()


async def test_a_failed_operation_is_still_http_200(ops: Ops) -> None:
    code, answer = await ops.route(
        {"kind": "lifecycle", "payload": {"path": "/garbage", "body": {}}}
    )

    assert code == 200
    assert answer["status"] == "failed"
    assert "not recognized" in answer["result"]["error"]


@pytest.mark.parametrize("body", [b"{not json", b'{"payload": {}}'])
async def test_a_malformed_body_is_a_400_and_is_never_admitted(ops: Ops, body: bytes) -> None:
    code, answer = await ops.route(body)

    assert code == 400
    assert "error" in answer
    assert ops.requests == set()
    assert ops.active_ops == {}


async def test_an_unknown_kind_is_a_failed_result_not_an_http_error(ops: Ops) -> None:
    code, answer = await ops.route({"kind": "bogus_kind", "payload": {}})

    assert code == 200
    assert answer == {"status": "failed", "result": {"error": "unknown kind: 'bogus_kind'"}}


# meiva:note
# A pool the daemon lost cannot serve a launch; the failure is not a modeled outcome, so it
# is logged with its traceback and still answers the caller.
async def test_a_crash_inside_dispatch_becomes_a_failed_result_and_is_logged(
    ops: Ops, agent_id: int, caplog: pytest.LogCaptureFixture
) -> None:
    closed: ConnectionPool = ConnectionPool(open=False)
    launch = LaunchAgentRequest(
        agent_id=agent_id, launch_attempt_id=UUID("00000000-0000-0000-0000-000000000001")
    )

    with caplog.at_level(logging.ERROR, logger="services.agent_runner.agent_ops.daemon"):
        code, answer = await replace(ops, pool=closed).route(
            {"kind": "spawn-launch", "payload": launch.model_dump(mode="json")}
        )

    assert code == 200
    assert answer["status"] == "failed"
    assert "PoolClosed" in answer["result"]["error"]
    assert "dispatch refused or crashed for kind=spawn-launch" in caplog.text


async def test_an_envelope_with_a_key_is_deduplicated(
    ops: Ops, db_conn: psycopg.Connection
) -> None:
    envelope = {
        "kind": "config_write",
        "payload": config_write("route-key"),
        "idempotency_key": "route-key",
    }

    first = await ops.route(envelope)
    second = await ops.route(envelope)

    assert first == second
    assert first[1]["status"] == "completed"
    assert executions("route-key", env_path=ops.authority.env_path) == 1


async def test_an_envelope_without_a_key_is_not_deduplicated(
    ops: Ops, db_conn: psycopg.Connection
) -> None:
    envelope = {"kind": "config_write", "payload": config_write("no-key")}

    await ops.route(envelope)
    await ops.route(envelope)

    assert executions("no-key", env_path=ops.authority.env_path) == 2
    assert db_conn.execute(
        "SELECT count(*) FROM api_idempotency WHERE method = 'ops'"
    ).fetchone() == (0,)


async def test_requests_stay_counted_until_their_operation_finishes(ops: Ops) -> None:
    envelope = {"kind": "config_write", "payload": config_write("counted")}

    with held_env_lock():
        tasks = [asyncio.create_task(ops.route(envelope)) for _ in range(2)]
        await _until(lambda: len(ops.requests) == 2)
        await asyncio.sleep(0.2)
        assert len(ops.requests) == 2
        assert not any(task.done() for task in tasks)

    await asyncio.gather(*tasks)
    assert ops.requests == set()
    assert executions("counted", env_path=ops.authority.env_path) == 2


@pytest.mark.parametrize(("cap", "unrelated_runs"), [(1, False), (2, True)])
async def test_the_semaphore_caps_how_many_requests_dispatch_at_once(
    ops: Ops, cap: int, unrelated_runs: bool
) -> None:
    stuck = {"kind": "config_write", "payload": config_write(f"cap-{cap}")}
    unrelated: dict[str, object] = {"kind": "config_read", "payload": {}}
    semaphore = asyncio.Semaphore(cap)

    with held_env_lock():
        first = asyncio.create_task(ops.route_raw(_encode(stuck), semaphore))
        await _until(lambda: "config_write" in ops.active_ops)
        second = asyncio.create_task(ops.route_raw(_encode(unrelated), semaphore))
        await asyncio.sleep(0.3)

        assert second.done() is unrelated_runs
        assert not first.done()

    await asyncio.gather(first, second)


# meiva:note
# The 2026-08-12 incident in one assertion. A blocking op on a runner stopped returning;
# because the arm ran inline on the loop it took the whole daemon with it — 2 h 03 m with not
# one line logged, every controller stopped, and the stranded-pause self-heal that exists for
# exactly that situation unable to run.
#
# The op here blocks until the test releases it. What must stay true is that the loop keeps
# turning meanwhile: other coroutines run, other ops dispatch, and the health endpoint's `await`
# still gets its turn.
async def test_a_blocking_operation_does_not_freeze_the_event_loop(ops: Ops) -> None:
    with held_env_lock():
        stuck = asyncio.create_task(ops.dispatch("config_write", config_write("loop-free")))
        await _until(lambda: "config_write" in ops.active_ops)

        for _ in range(5):
            await asyncio.sleep(0)
        status, _result = await asyncio.wait_for(ops.dispatch("config_read", {}), 10)

        assert status == "completed"
        assert not stuck.done()

    await stuck


def _encode(envelope: Mapping[str, object]) -> bytes:
    return json.dumps(envelope).encode()
