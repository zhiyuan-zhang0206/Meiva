# meiva:note
# Actual request and executor lifetime must outlive a disconnected awaiter, and a SIGTERMed
# ops server drains what it admitted before it exits.

import asyncio
import gc
import os
import signal
import socket
import struct
import threading
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.daemon.health import stop_health_server
from base.daemon.http_transport import start_daemon_http
from base.db import Database
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from services.agent_runner.agent_ops import boot, daemon, health
from services.agent_runner.agent_ops import in_flight as activity


async def test_same_kind_requests_remain_counted_until_they_finish(
    monkeypatch: pytest.MonkeyPatch,
    op_executor: ThreadPoolExecutor,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    requests: activity.RequestTokens = set()
    workers: activity.WorkerFutures = set()
    finishes = [asyncio.Event(), asyncio.Event()]
    entered = asyncio.Event()
    count = 0

    async def dispatch(
        *_args: Any, local_machine: MachineIdentity, **_kwargs: Any
    ) -> tuple[str, dict[str, object]]:
        del local_machine
        nonlocal count
        index = count
        count += 1
        if count == 2:
            entered.set()
        await finishes[index].wait()
        return "completed", {}

    dispatch_sem = asyncio.Semaphore(3)
    monkeypatch.setattr(daemon, "_dispatch", dispatch)
    tasks = [
        asyncio.create_task(
            daemon.ops_route(
                b'{"kind":"config_read","payload":{}}',
                active_ops={},
                dispatch_sem=dispatch_sem,
                workers=workers,
                requests=requests,
                pool=dispatch_pool,
                executor=op_executor,
                catalog=model_catalog,
                authority=config_authority,
                database=ops_database,
                image=ops_image,
                local_machine=machine_identity,
                host=fake_host(),
            )
        )
        for _ in range(2)
    ]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert len(requests) == 2
        finishes[0].set()
        await tasks[0]
        assert len(requests) == 1
        assert count == 2
    finally:
        for event in finishes:
            event.set()
        await asyncio.gather(*tasks)
    assert len(requests) == 0


async def test_cancelled_same_kind_await_does_not_hide_running_executor(
    monkeypatch: pytest.MonkeyPatch,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    workers: activity.WorkerFutures = set()
    active_ops: daemon.ActiveOps = {}
    entered = [threading.Event(), threading.Event()]
    finish = [threading.Event(), threading.Event()]

    def arm(
        _kind: str,
        payload: dict[str, Any],
        *,
        pool: ConnectionPool,
        authority: ConfigAuthority,
        database: Callable[[], Database],
        image: LoadedCommit,
        local_machine: MachineIdentity,
        host: Host,
    ) -> tuple[str, dict[str, object]]:
        del local_machine
        assert pool is dispatch_pool
        index = int(payload["index"])
        entered[index].set()
        if not finish[index].wait(5):
            raise TimeoutError("test worker was not released")
        return "completed", {}

    with ThreadPoolExecutor(max_workers=2) as executor:
        monkeypatch.setattr(daemon, "bind_dispatch_sync", arm)
        tasks = [
            asyncio.create_task(
                daemon.run_arm(
                    "same",
                    {"index": index},
                    active_ops=active_ops,
                    workers=workers,
                    pool=dispatch_pool,
                    executor=executor,
                    authority=config_authority,
                    database=ops_database,
                    image=ops_image,
                    local_machine=machine_identity,
                    host=fake_host(),
                )
            )
            for index in range(2)
        ]
        try:
            async with asyncio.timeout(2):
                while not all(event.is_set() for event in entered):
                    await asyncio.sleep(0.01)
            assert len(workers) == 2
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            assert len(workers) == 2
        finally:
            for event in finish:
                event.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            async with asyncio.timeout(2):
                while workers:
                    await asyncio.sleep(0.01)


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


async def test_active_ops_share_health_and_cleanup_within_one_daemon(
    monkeypatch: pytest.MonkeyPatch,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    workers: activity.WorkerFutures = set()
    active_ops: daemon.ActiveOps = {}
    other_daemon: daemon.ActiveOps = {}
    entered = [threading.Event(), threading.Event()]
    finish = [threading.Event(), threading.Event()]

    def arm(
        _kind: str,
        payload: dict[str, Any],
        *,
        pool: ConnectionPool,
        authority: ConfigAuthority,
        database: Callable[[], Database],
        image: LoadedCommit,
        local_machine: MachineIdentity,
        host: Host,
    ) -> tuple[str, dict[str, object]]:
        del local_machine
        assert pool is dispatch_pool
        index = int(payload["index"])
        entered[index].set()
        if not finish[index].wait(5):
            raise TimeoutError("test worker was not released")
        if index == 1:
            raise RuntimeError("operation failed")
        return "completed", {}

    with ThreadPoolExecutor(max_workers=2) as executor:
        monkeypatch.setattr(daemon, "bind_dispatch_sync", arm)
        tasks = [
            asyncio.create_task(
                daemon.run_arm(
                    kind,
                    {"index": index},
                    active_ops=active_ops,
                    workers=workers,
                    pool=dispatch_pool,
                    executor=executor,
                    authority=config_authority,
                    database=ops_database,
                    image=ops_image,
                    local_machine=machine_identity,
                    host=fake_host(),
                )
            )
            for index, kind in enumerate(("config_read", "inventory_read"))
        ]
        try:
            async with asyncio.timeout(2):
                while not all(event.is_set() for event in entered):
                    await asyncio.sleep(0.01)
            assert health.ops_components(active_ops)[1]["progress"] == "2 active"
            assert health.saturation(active_ops, 2) == 1.0
            assert health.ops_components(other_daemon)[1]["progress"] == "0 active"
            finish[0].set()
            await tasks[0]
            assert set(active_ops) == {"inventory_read"}
            assert health.saturation(active_ops, 2) == 0.5
            finish[1].set()
            with pytest.raises(RuntimeError, match="operation failed"):
                await tasks[1]
            assert active_ops == {}
            assert health.ops_components(active_ops)[1]["progress"] == "0 active"
            assert health.saturation(active_ops, 2) == 0.0
        finally:
            for event in finish:
                event.set()
            await asyncio.gather(*tasks, return_exceptions=True)


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


async def test_op_invocations_dispatch_on_their_own_executor(
    monkeypatch: pytest.MonkeyPatch,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)

    def arm(
        _kind: str,
        _payload: dict[str, Any],
        *,
        pool: ConnectionPool,
        authority: ConfigAuthority,
        database: Callable[[], Database],
        image: LoadedCommit,
        local_machine: MachineIdentity,
        host: Host,
    ) -> tuple[str, dict[str, object]]:
        del local_machine
        assert pool is dispatch_pool
        return "completed", {"thread": threading.current_thread().name}

    monkeypatch.setattr(daemon, "bind_dispatch_sync", arm)
    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="first-owner") as first,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="second-owner") as second,
    ):
        first_result = await daemon.run_arm(
            "status_probe",
            {},
            active_ops={},
            workers=set(),
            pool=dispatch_pool,
            executor=first,
            authority=config_authority,
            database=ops_database,
            image=ops_image,
            local_machine=machine_identity,
            host=fake_host(),
        )
        boot.shutdown_op_pool(first)
        second_result = await daemon.run_arm(
            "status_probe",
            {},
            active_ops={},
            workers=set(),
            pool=dispatch_pool,
            executor=second,
            authority=config_authority,
            database=ops_database,
            image=ops_image,
            local_machine=machine_identity,
            host=fake_host(),
        )
    first_thread = first_result[1]["thread"]
    second_thread = second_result[1]["thread"]
    assert isinstance(first_thread, str) and first_thread.startswith("first-owner")
    assert isinstance(second_thread, str) and second_thread.startswith("second-owner")


async def test_drain_returns_at_once_when_nothing_is_in_flight() -> None:
    async with asyncio.timeout(1):
        await activity.drain(requests=set(), workers=set(), budget_s=5)


async def test_drain_waits_for_an_admitted_request_to_finish() -> None:
    requests: activity.RequestTokens = set()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def request() -> None:
        with activity.admitted(requests=requests):
            entered.set()
            await finish.wait()

    task = asyncio.create_task(request())
    await entered.wait()
    draining = asyncio.create_task(activity.drain(requests=requests, workers=set(), budget_s=5))
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

    await activity.drain(requests=requests, workers=workers, budget_s=0.2)

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
        activity.serve_until_sigterm(server, requests=requests, workers=workers)
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
