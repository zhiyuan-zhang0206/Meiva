import asyncio
import threading
from typing import Any

import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from gateway.tests.auth.gateway_config import gateway_test_app, gateway_test_process


async def test_lifespan_waits_for_cancelled_request_sql_before_pool_close(
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    del db_conn
    ready = asyncio.Event()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    with gateway_test_process(machine=machine_identity) as process:
        app = gateway_test_app(process=process)

        async def service() -> None:
            async with app.router.lifespan_context(app):
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
                        "native-sql", query, ttl_s=0, now=0.0
                    )
                )
                assert await asyncio.to_thread(started.wait, 1)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                running.cancel()
                # meiva:note
                # The cancelled request's SQL is still running: the lifespan must not have
                # unwound past it, so neither pool is closed yet.
                assert not (await asyncio.wait({running}, timeout=0.02))[0]
                assert not app.state.db_pool.closed
                assert not app.state.control_db_pool.closed
                assert not finished.is_set()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await running
                assert finished.is_set()
                assert app.state.db_pool.closed
                assert app.state.control_db_pool.closed
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
    ready, stop = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    reports: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reports.append(context))

    with gateway_test_process() as process:
        app = gateway_test_app(process=process)

        async def service() -> None:
            async with app.router.lifespan_context(app):
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
                        "bad-sql", query, ttl_s=0, now=0.0
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
