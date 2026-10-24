# meiva:note
# The gateway lifespan against its real resources.
#
# The app is `create_app`'s own and its lifespan is `app.router.lifespan_context`; pools, clients,
# the runtime monitor and the flushers are the production objects. The flushers are found by the
# names the lifespan gives their tasks. The release policy (last acquired first, a failing release
# never skips the rest, the start-up error survives) is the component
# `gateway.app.startup_releases`, asserted in `test_startup_releases.py`; the tests here pin that the
# lifespan registers what it acquires with it, through the failures a real gateway meets: an
# unusable setting after the pools opened, a failure while serving, and a stop that arrives while a
# physical query is still running.

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.tests.configuration import snapshot_process_config
from base.host.system.tests.fake_commands import fake_host
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home
from gateway import app as gateway_app
from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess
from gateway.http.config import gateway_config_from_boot
from gateway.http.model_catalog import load_model_catalog
from gateway.tests.auth.gateway_config import gateway_test_app, gateway_test_process

_FLUSHERS = ("gateway-latency-flusher", "gateway-auth-flusher")
_BACKGROUND = (*_FLUSHERS, "gateway-notification-outbox")


def _running(*names: str) -> set[asyncio.Task[Any]]:
    return {task for task in asyncio.all_tasks() if task.get_name() in names and not task.done()}


async def test_overlapping_lifespans_join_their_original_flushers(
    db_conn: psycopg.Connection[Any], *, machine_identity: MachineIdentity
) -> None:
    del db_conn
    with gateway_test_process(machine=machine_identity) as process:
        app = gateway_test_app(process=process)
        async with asyncio.timeout(5), app.router.lifespan_context(app):
            pipeline = process.event_pipeline()
            assert app.state.gateway_process is process
            assert app.state.machine is machine_identity
            assert app.state.process_image is process.image
            original = _running(*_FLUSHERS)
            assert len(original) == 2
            async with app.router.lifespan_context(app):
                assert process.event_pipeline() is pipeline
                replacement = _running(*_FLUSHERS) - original
                assert len(replacement) == 2
            assert not pipeline.stopped
            assert all(task.done() for task in replacement)
            assert all(not task.done() for task in original)
        assert all(task.done() for task in original)
        assert not pipeline.stopped
    assert pipeline.stopped


async def test_process_ownership_borrowed_vs_owned_controls_client_close_count(
    db_conn: psycopg.Connection[Any], *, machine_identity: MachineIdentity
) -> None:
    del db_conn
    with gateway_test_process(machine=machine_identity) as borrowed_process:
        borrowed_app = gateway_test_app(process=borrowed_process)
        async with asyncio.timeout(5), borrowed_app.router.lifespan_context(borrowed_app):
            assert borrowed_process.clients.exports_deferred() is not None
        assert borrowed_process.clients.exports_deferred() is not None
    assert borrowed_process.clients.exports_deferred() is None

    owned_process = GatewayProcess(
        home=meiva_home(),
        config=snapshot_process_config(),
        image=LOADED_IMAGE,
        machine=machine_identity,
        host=fake_host(),
    )
    owned_app = gateway_test_app(process=owned_process, process_ownership="owned")
    async with asyncio.timeout(5), owned_app.router.lifespan_context(owned_app):
        assert owned_process.clients.exports_deferred() is not None
    assert owned_process.clients.exports_deferred() is None


# meiva:note
# A home that is not an absolute path cannot hold the unit's `.env`: start-up fails after
# the owned process built its event writer, and that writer is closed on the way out.
async def test_owned_process_startup_failure_closes_its_clients_and_raises_the_failure(
    *, machine_identity: MachineIdentity
) -> None:
    process = GatewayProcess(
        home=Path("relative-home"),
        config=snapshot_process_config(),
        image=LOADED_IMAGE,
        machine=machine_identity,
        host=fake_host(),
    )
    app = gateway_test_app(process=process, process_ownership="owned")
    with pytest.raises(ValueError, match="env_path must be absolute"):
        async with app.router.lifespan_context(app):
            pytest.fail("the lifespan served with an unusable home")
    assert process.clients.exports_deferred() is None


def test_invalid_process_ownership_rejected(*, machine_identity: MachineIdentity) -> None:
    boot = snapshot_process_config()
    process = GatewayProcess(
        home=meiva_home(),
        config=boot,
        image=LOADED_IMAGE,
        machine=machine_identity,
        host=fake_host(),
    )
    bad_ownership: Any = "invalid"
    with pytest.raises(ValueError, match="gateway process ownership must be borrowed or owned"):
        gateway_app.create_app(
            gateway_config_from_boot(boot),
            process=process,
            process_ownership=bad_ownership,
            catalog=load_model_catalog(build_model_catalog),
        )


# meiva:note
# Each background task finishes while both pools are still open, and every resource the
# lifespan acquired is closed once the cancelled lifespan has unwound.
async def test_cancelled_lifespan_stops_its_tasks_before_the_pools_close(
    db_conn: psycopg.Connection[Any], *, machine_identity: MachineIdentity
) -> None:
    del db_conn
    with gateway_test_process(machine=machine_identity) as process:
        app = gateway_test_app(process=process)
        entered = asyncio.Event()
        pools_open_when_done: list[tuple[str, bool]] = []

        async def serve() -> None:
            async with app.router.lifespan_context(app):
                entered.set()
                await asyncio.Event().wait()

        async with asyncio.timeout(5):
            running = asyncio.create_task(serve())
            await entered.wait()
            tasks = _running(*_BACKGROUND)
            assert {task.get_name() for task in tasks} == set(_BACKGROUND)
            for task in tasks:
                task.add_done_callback(
                    lambda done: pools_open_when_done.append(
                        (
                            done.get_name(),
                            not app.state.db_pool.closed and not app.state.control_db_pool.closed,
                        )
                    )
                )
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running

    assert sorted(pools_open_when_done) == sorted((name, True) for name in _BACKGROUND)
    assert app.state.db_pool.closed
    assert app.state.control_db_pool.closed
    assert app.state.grafana_client.is_closed
    assert app.state.insights_client.is_closed


async def test_background_tasks_finish_before_the_pools_close_on_a_normal_exit(
    db_conn: psycopg.Connection[Any], *, machine_identity: MachineIdentity
) -> None:
    del db_conn
    with gateway_test_process(machine=machine_identity) as process:
        app = gateway_test_app(process=process)
        pools_open_when_done: list[bool] = []
        async with asyncio.timeout(5), app.router.lifespan_context(app):
            for task in _running(*_BACKGROUND):
                task.add_done_callback(
                    lambda _done: pools_open_when_done.append(not app.state.db_pool.closed)
                )
    assert pools_open_when_done == [True] * len(_BACKGROUND)
    assert app.state.db_pool.closed


# meiva:note
# A setting that cannot start the gateway, met after the data and control pools opened and the
# inspection cache was registered: the memory-search gate is built from a limit no semaphore
# accepts. The error is the one the caller sees, and every resource acquired before it is closed.
async def test_a_start_up_failure_after_the_pools_opened_closes_everything_acquired(
    db_conn: psycopg.Connection[Any], *, machine_identity: MachineIdentity
) -> None:
    del db_conn
    with gateway_test_process(
        machine=machine_identity, fields={"memory_search_max_concurrency": -1}
    ) as process:
        app = gateway_test_app(process=process)
        with pytest.raises(ValueError, match="Semaphore initial value must be >= 0"):
            async with asyncio.timeout(5), app.router.lifespan_context(app):
                pytest.fail("the lifespan served with an unusable memory-search limit")

        assert app.state.db_pool.closed
        assert app.state.control_db_pool.closed
        assert not _running(*_BACKGROUND)
        with pytest.raises(RuntimeError, match="inspection query owner is closed"):
            await app.state.inspect_query_cache.get_or_load_async(
                ("closed", None), lambda: 0, ttl_s=0, now=time.monotonic()
            )


# meiva:note
# A failure raised while the gateway is serving, with every resource acquired and every
# background task running: the tasks end first, while both pools are still open, then the pools
# and clients close, and the failure is the error the caller sees.
async def test_a_failure_while_serving_ends_the_tasks_before_closing_the_pools(
    db_conn: psycopg.Connection[Any], *, machine_identity: MachineIdentity
) -> None:
    del db_conn
    failure = RuntimeError("serving failed")
    with gateway_test_process(machine=machine_identity) as process:
        app = gateway_test_app(process=process)
        pools_open_when_done: list[bool] = []
        with pytest.raises(ExceptionGroup) as raised:
            async with asyncio.timeout(5), app.router.lifespan_context(app):
                for task in _running(*_BACKGROUND):
                    task.add_done_callback(
                        lambda _done: pools_open_when_done.append(
                            not app.state.db_pool.closed and not app.state.control_db_pool.closed
                        )
                    )
                raise failure

    assert raised.value.exceptions == (failure,)
    assert pools_open_when_done == [True] * len(_BACKGROUND)
    assert app.state.db_pool.closed
    assert app.state.control_db_pool.closed
    assert app.state.grafana_client.is_closed
    assert app.state.insights_client.is_closed
    assert app.state.mcp_manager is None


# meiva:note
# An inspection query still running when the gateway stops finishes against an open pool: the
# lifespan registers the inspection cache after the data pool, so it is drained first. The query
# borrows a connection, signals, runs for a while, then borrows another one; that second borrow
# fails if the pool closed while the query was away.
async def test_an_inspection_query_in_flight_finishes_before_the_data_pool_closes(
    db_conn: psycopg.Connection[Any], *, machine_identity: MachineIdentity
) -> None:
    del db_conn
    running = threading.Event()

    with gateway_test_process(machine=machine_identity) as process:
        app = gateway_test_app(process=process)

        def query() -> int:
            with app.state.db_pool.connection() as conn:
                running.set()
                conn.execute("SELECT pg_sleep(0.5)")
            with app.state.db_pool.connection() as conn:
                row = conn.execute("SELECT 1").fetchone()
            assert row is not None
            return int(row[0])

        async with asyncio.timeout(10):
            async with app.router.lifespan_context(app):
                load = asyncio.create_task(
                    app.state.inspect_query_cache.get_or_load_async(
                        ("in-flight", None), query, ttl_s=0, now=time.monotonic()
                    )
                )
                assert await asyncio.to_thread(running.wait, 5)
            assert await load == 1
    assert app.state.db_pool.closed
