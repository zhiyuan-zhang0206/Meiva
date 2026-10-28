# meiva:note
# Actual request and executor lifetime must outlive a disconnected awaiter, and a SIGTERMed
# ops server drains what it admitted before it exits.

import asyncio
import gc
import os
import signal
import socket
import struct
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from pydantic import ValidationError

from base.clock import SYSTEM_TIMERS
from base.clock.tests.manual_timers import ManualTimers
from base.daemon.health import stop_health_server
from base.daemon.http_transport import start_daemon_http
from services.agent_runner.agent_ops import boot, health
from services.agent_runner.agent_ops import in_flight as activity
from services.agent_runner.agent_ops.tests.ops_support import Ops, config_write, held_env_lock


async def _until(condition: Callable[[], bool], *, timeout_s: float = 10.0) -> None:
    for _ in range(int(timeout_s / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never held")


async def test_a_cancelled_await_does_not_hide_the_running_worker(ops: Ops) -> None:
    with held_env_lock():
        tasks = [
            asyncio.create_task(ops.run_arm("config_write", config_write(f"worker-{index}")))
            for index in range(2)
        ]
        await _until(lambda: len(ops.workers) == 2)

        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]

        assert len(ops.workers) == 2

    await asyncio.gather(*tasks, return_exceptions=True)
    await _until(lambda: not ops.workers)


async def test_server_close_after_client_reset_is_not_request_completion() -> None:
    requests: activity.RequestTokens = set()
    entered, finish, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def route(_body: bytes) -> tuple[int, bytes, str]:
        with activity.admitted(requests=requests):
            entered.set()
            await finish.wait()
            returned.set()
            return 200, b"{}", "application/json"

    server = await start_daemon_http(
        host="127.0.0.1",
        port=0,
        health_response=lambda: (200, b"{}"),
        extra_routes={("POST", "/ops"): route},
    )
    peer = socket.socket()
    peer.setblocking(False)
    loop = asyncio.get_running_loop()
    try:
        await loop.sock_connect(peer, ("127.0.0.1", server.sockets[0].getsockname()[1]))
        await loop.sock_sendall(
            peer, b"POST /ops HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}"
        )
        await asyncio.wait_for(entered.wait(), 2)
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        peer.close()
        await asyncio.wait_for(stop_health_server(server), 2)
        assert not returned.is_set()
        assert len(requests) == 1
    finally:
        peer.close()
        finish.set()
        await asyncio.wait_for(returned.wait(), 2)
        await stop_health_server(server)
    assert len(requests) == 0


async def test_active_ops_share_health_and_cleanup_within_one_daemon(ops: Ops) -> None:
    other_daemon: dict[str, tuple[str, float]] = {}

    with held_env_lock():
        tasks = [
            asyncio.create_task(ops.run_arm("config_write", config_write("active"))),
            asyncio.create_task(ops.run_arm("inventory_write", {"plugins": {}, "mcp_servers": {}})),
        ]
        await _until(lambda: set(ops.active_ops) == {"config_write", "inventory_write"})

        assert health.ops_components(ops.active_ops)[1]["progress"] == "2 active"
        assert health.saturation(ops.active_ops, 2) == 1.0
        assert health.ops_components(other_daemon)[1]["progress"] == "0 active"

    await asyncio.gather(*tasks)
    assert ops.active_ops == {}
    assert health.ops_components(ops.active_ops)[1]["progress"] == "0 active"
    assert health.saturation(ops.active_ops, 2) == 0.0


async def test_a_failing_operation_leaves_no_active_entry(ops: Ops) -> None:
    with pytest.raises(ValidationError):
        await ops.run_arm("config_audit_read", {"last": "not a number"})

    assert ops.active_ops == {}
    assert ops.workers == set()


async def test_operations_are_scheduled_on_the_executor_of_their_invocation(ops: Ops) -> None:
    first = ThreadPoolExecutor(max_workers=1, thread_name_prefix="first-owner")
    second = ThreadPoolExecutor(max_workers=1, thread_name_prefix="second-owner")
    try:
        assert (await replace(ops, executor=first).run_arm("config_read", {}))[0] == "completed"
        boot.shutdown_op_pool(first)

        with pytest.raises(RuntimeError, match="shutdown"):
            await replace(ops, executor=first).run_arm("config_read", {})
        assert (await replace(ops, executor=second).run_arm("config_read", {}))[0] == "completed"
    finally:
        second.shutdown(wait=True)


async def test_worker_future_is_retained_until_completion_and_isolated_per_daemon() -> None:
    workers: activity.WorkerFutures = set()
    other_workers: activity.WorkerFutures = set()
    future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
    reference = weakref.ref(future)
    activity.track_worker(future, workers=workers)
    del future
    gc.collect()
    retained = reference()
    assert retained is not None
    assert len(workers) == 1
    assert len(other_workers) == 0
    retained.set_result(None)
    await asyncio.sleep(0)
    assert len(workers) == 0


# meiva:note
# The drain spends the budget of the timers it was given: a manual clock runs the loop to its
# deadline without real time passing, and a drain that read the loop's own clock instead would
# leave `sleeps` empty and never reach the deadline inside the test's bound.
async def test_drain_spends_the_budget_of_the_timers_it_was_given() -> None:
    timers = ManualTimers()
    requests: activity.RequestTokens = {asyncio.ensure_future(asyncio.sleep(30))}
    try:
        await asyncio.wait_for(
            activity.drain(requests=requests, workers=set(), timers=timers, budget_s=5),
            timeout=1,
        )
    finally:
        for task in requests:
            task.cancel()
    assert timers.sleeps, "the drain slept on a clock other than the one it was given"
    assert sum(timers.sleeps) >= 5


async def test_drain_returns_at_once_when_nothing_is_in_flight() -> None:
    async with asyncio.timeout(1):
        await activity.drain(requests=set(), workers=set(), timers=SYSTEM_TIMERS, budget_s=5)


async def test_drain_waits_for_an_admitted_request_to_finish() -> None:
    requests: activity.RequestTokens = set()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def request() -> None:
        with activity.admitted(requests=requests):
            entered.set()
            await finish.wait()

    task = asyncio.create_task(request())
    await entered.wait()
    draining = asyncio.create_task(
        activity.drain(requests=requests, workers=set(), timers=SYSTEM_TIMERS, budget_s=5)
    )
    await asyncio.sleep(0.25)
    assert not draining.done(), "the drain returned while a request was still admitted"
    finish.set()
    async with asyncio.timeout(2):
        await draining
    await task


async def test_drain_names_what_is_left_when_the_budget_is_spent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: activity.RequestTokens = set()
    workers: activity.WorkerFutures = {asyncio.get_running_loop().create_future()}
    entered = asyncio.Event()

    async def stuck_request() -> None:
        with activity.admitted(requests=requests):
            entered.set()
            await asyncio.Event().wait()

    handler = asyncio.create_task(stuck_request())
    await entered.wait()

    await activity.drain(requests=requests, workers=workers, timers=SYSTEM_TIMERS, budget_s=0.2)

    (record,) = [r for r in caplog.records if "stop drain budget" in r.getMessage()]
    assert record.levelname == "ERROR"
    assert "1 request(s) and 1 worker(s)" in record.getMessage()
    with pytest.raises(asyncio.CancelledError):
        await handler
    assert not requests, "the cancelled handler left its admission"


async def test_sigterm_closes_the_server_to_new_connections_and_waits_for_admitted_requests() -> (
    None
):
    requests: activity.RequestTokens = set()
    workers: activity.WorkerFutures = set()
    entered, finish, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def route(_body: bytes) -> tuple[int, bytes, str]:
        with activity.admitted(requests=requests):
            entered.set()
            await finish.wait()
            returned.set()
            return 200, b"{}", "application/json"

    server = await start_daemon_http(
        host="127.0.0.1",
        port=0,
        health_response=lambda: (200, b"{}"),
        extra_routes={("POST", "/ops"): route},
    )
    port = server.sockets[0].getsockname()[1]
    loop = asyncio.get_running_loop()
    peer = socket.socket()
    peer.setblocking(False)
    previous = signal.getsignal(signal.SIGTERM)
    serving = asyncio.create_task(
        activity.serve_until_sigterm(
            server, requests=requests, workers=workers, timers=SYSTEM_TIMERS
        )
    )
    try:
        await loop.sock_connect(peer, ("127.0.0.1", port))
        await loop.sock_sendall(
            peer, b"POST /ops HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}"
        )
        await asyncio.wait_for(entered.wait(), 2)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(0.3)
        assert not serving.done(), "the server exited while a request was still admitted"
        late = socket.socket()
        late.setblocking(False)
        try:
            with pytest.raises(OSError):
                await asyncio.wait_for(loop.sock_connect(late, ("127.0.0.1", port)), 1)
        finally:
            late.close()
        finish.set()
        await asyncio.wait_for(serving, 5)
        assert returned.is_set()
    finally:
        finish.set()
        peer.close()
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        signal.signal(signal.SIGTERM, previous)
        await stop_health_server(server)


# meiva:note
# A retried SIGTERM that the loop handles in the same iteration as the first is one stop, not a
# second cancellation: the serving task would otherwise be cancelled twice, absorb one and
# propagate the other, and the daemon would exit through a traceback instead of draining.
async def test_two_sigterms_handled_together_are_one_stop() -> None:
    requests: activity.RequestTokens = set()
    workers: activity.WorkerFutures = set()
    server = await start_daemon_http(
        host="127.0.0.1", port=0, health_response=lambda: (200, b"{}"), extra_routes={}
    )
    previous = signal.getsignal(signal.SIGTERM)
    serving = asyncio.create_task(
        activity.serve_until_sigterm(
            server, requests=requests, workers=workers, timers=SYSTEM_TIMERS
        )
    )
    try:
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(serving, 5)
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        signal.signal(signal.SIGTERM, previous)
        await stop_health_server(server)
