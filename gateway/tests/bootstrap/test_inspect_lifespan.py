import asyncio
import threading
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.tests.configuration import snapshot_process_config
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host
from base.paths import meiva_home
from gateway import app as gateway_app
from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess
from gateway.http.config import gateway_config_from_boot
from gateway.tests.auth.gateway_config import gateway_test_process


class _CheckedPool:
    def __init__(self, pool: Any, on_close: Callable[[], None]) -> None:
        self._pool = pool
        self._on_close = on_close

    def close(self) -> None:
        self._on_close()
        self._pool.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)


class _CheckedDatabase:
    def __init__(self, database: Any, on_close: Callable[[], None]) -> None:
        self._database = database
        self._on_close = on_close

    def pool(self, **kwargs: Any) -> Any:
        return _CheckedPool(self._database.pool(**kwargs), self._on_close)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._database, name)


class _CheckedPoolsProcess(GatewayProcess):
    def __init__(self, *, machine: MachineIdentity, on_close: Callable[[], None]) -> None:
        self._on_close = on_close
        super().__init__(
            home=meiva_home(),
            config=snapshot_process_config(),
            image=LOADED_IMAGE,
            machine=machine,
            host=fake_host(),
        )

    def database(self) -> Any:
        return _CheckedDatabase(super().database(), self._on_close)


async def test_lifespan_waits_for_cancelled_request_sql_before_pool_close(
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentity,
) -> None:
    del db_conn

    def fake_register_os_cron(*, host: Host, enabled_reader: Callable[[], bool]) -> None:
        del host, enabled_reader

    app = FastAPI()
    ready = asyncio.Event()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    pool_closed_after_sql: list[bool] = []
    process = _CheckedPoolsProcess(
        machine=machine_identity,
        on_close=lambda: pool_closed_after_sql.append(finished.is_set()),
    )

    with process.lifetime():
        app.state.gateway_config = gateway_config_from_boot(process.config)
        app.state.os_cron_registrar = fake_register_os_cron

        async def service() -> None:
            async with gateway_app.lifespan(app, process=process, process_ownership="borrowed"):
                ready.set()
                await asyncio.Event().wait()

        def query() -> int:
            with app.state.db_pool.connection(timeout=1) as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
                started.set()
                assert release.wait(timeout=5)
                result = conn.execute("SELECT 2").fetchone()[0]
            finished.set()
            return result

        running = asyncio.create_task(service())
        request = None
        try:
            async with asyncio.timeout(10):
                await ready.wait()
                request = asyncio.create_task(
                    app.state.inspect_query_cache.get_or_load_async(
                        "native-sql", query, ttl_s=0, now=lambda: 0
                    )
                )
                assert await asyncio.to_thread(started.wait, 1)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                running.cancel()
                assert not (await asyncio.wait({running}, timeout=0.02))[0]
                assert pool_closed_after_sql == []
                assert not finished.is_set()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await running
                assert finished.is_set()
                assert pool_closed_after_sql == [True, True]
        finally:
            release.set()
            running.cancel()
            await asyncio.gather(
                *[task for task in (running, request) if task], return_exceptions=True
            )


async def test_native_sql_error_does_not_cancel_gateway_service(
    db_conn: psycopg.Connection[Any],
) -> None:
    del db_conn

    def fake_register_os_cron(*, host: Host, enabled_reader: Callable[[], bool]) -> None:
        del host, enabled_reader

    app = FastAPI()
    ready, stop = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    reports: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reports.append(context))

    with gateway_test_process() as process:
        app.state.gateway_config = gateway_config_from_boot(process.config)
        app.state.os_cron_registrar = fake_register_os_cron

        async def service() -> None:
            async with gateway_app.lifespan(app, process=process, process_ownership="borrowed"):
                ready.set()
                await stop.wait()

        def query() -> None:
            with app.state.db_pool.connection(timeout=1) as conn:
                conn.execute("SELECT * FROM inspection_lifetime_missing_table")

        running = asyncio.create_task(service())
        try:
            async with asyncio.timeout(10):
                await ready.wait()
                with pytest.raises(psycopg.errors.UndefinedTable) as raised:
                    await app.state.inspect_query_cache.get_or_load_async(
                        "bad-sql", query, ttl_s=0, now=lambda: 0
                    )
                assert not running.done()
                with app.state.db_pool.connection(timeout=1) as conn:
                    assert conn.execute("SELECT 1").fetchone()[0] == 1
                assert reports[0]["exception"] is raised.value
                stop.set()
                await running
        finally:
            stop.set()
            await asyncio.gather(running, return_exceptions=True)
            loop.set_exception_handler(previous)
