from __future__ import annotations

import asyncio
import functools
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import cast

import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from services.agent_runner.agent_ops import boot, daemon
from services.agent_runner.agent_ops.tests.test_daemon import (
    ops_pool as ops_pool,
)
from services.agent_runner.agent_ops.tests.test_daemon import stub_pool


@pytest.mark.asyncio
async def test_an_unrelated_op_still_dispatches_while_a_read_is_stuck(
    op_executor: ThreadPoolExecutor,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    dispatch_pool: ConnectionPool = stub_pool()
    started = threading.Event()
    release = threading.Event()

    def _wedged(*, local_machine: MachineIdentity, host: Host) -> object:
        del local_machine
        started.set()
        release.wait(timeout=30)
        from ops.rpc_schemas import InventoryReadResult

        return InventoryReadResult(machine="runner", plugins={}, mcp_servers={})

    class _Status:
        def model_dump(self: object, *, mode: str) -> dict[str, bool]:
            del mode
            return {"ready": True}

    def _status(
        _db: Database, _pool: object, *, image: LoadedCommit, local_machine: MachineIdentity
    ) -> _Status:
        del local_machine
        return _Status()

    dispatch_arm = functools.partial(
        daemon.bind_dispatch_sync,
        inventory_read_op=_wedged,
        cluster_status_op=_status,
    )

    stuck = asyncio.ensure_future(
        daemon.dispatch(
            "inventory_read",
            {},
            active_ops={},
            workers=set(),
            pool=dispatch_pool,
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
            database=ops_database,
            image=ops_image,
            local_machine=machine_identity,
            host=fake_host(),
            dispatch_arm=dispatch_arm,
        )
    )
    await asyncio.to_thread(started.wait, 10)

    status, result = await daemon.dispatch(
        "status_probe",
        {},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
        host=fake_host(),
        dispatch_arm=dispatch_arm,
    )
    assert (status, result) == ("completed", {"ready": True})

    release.set()
    await stuck


@pytest.mark.asyncio
async def test_two_config_writes_cannot_interleave(
    op_executor: ThreadPoolExecutor,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    dispatch_pool: ConnectionPool = stub_pool()
    inside = 0
    overlapped = False

    class _Result:
        def model_dump(self: object, **_kw: object) -> dict[str, object]:
            return {"ok": True}

    def _slow_write(*_a: object, local_machine: MachineIdentity, **_kw: object) -> _Result:
        del local_machine
        nonlocal inside, overlapped
        inside += 1
        if inside > 1:
            overlapped = True
        time.sleep(0.05)
        inside -= 1
        return _Result()

    dispatch_arm = functools.partial(
        daemon.bind_dispatch_sync,
        config_write_op=_slow_write,
        inventory_write_op=_slow_write,
    )

    await asyncio.gather(
        daemon.dispatch(
            "config_write",
            {"overrides": {}},
            active_ops={},
            workers=set(),
            pool=dispatch_pool,
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
            database=ops_database,
            image=ops_image,
            local_machine=machine_identity,
            host=fake_host(),
            dispatch_arm=dispatch_arm,
        ),
        daemon.dispatch(
            "config_write",
            {"overrides": {}},
            active_ops={},
            workers=set(),
            pool=dispatch_pool,
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
            database=ops_database,
            image=ops_image,
            local_machine=machine_identity,
            host=fake_host(),
            dispatch_arm=dispatch_arm,
        ),
        daemon.dispatch(
            "inventory_write",
            {"plugins": {}, "mcp_servers": {}},
            active_ops={},
            workers=set(),
            pool=dispatch_pool,
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
            database=ops_database,
            image=ops_image,
            local_machine=machine_identity,
            host=fake_host(),
            dispatch_arm=dispatch_arm,
        ),
    )

    assert not overlapped, "two state writes ran concurrently — .env can lose fields"


@pytest.mark.asyncio
async def test_config_write_op_receives_actor_and_trace(
    op_executor: ThreadPoolExecutor,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    captured: dict[str, object] = {}

    class _Result:
        def model_dump(self: object, **_kw: object) -> dict[str, object]:
            return {"ok": True}

    def _capture(*_a: object, local_machine: str, **_kw: object) -> _Result:
        del local_machine
        captured.update(_kw)
        return _Result()

    dispatch_pool: ConnectionPool = stub_pool()
    host = fake_host()
    status, _ = await daemon.dispatch(
        "config_write",
        {"overrides": {}, "actor": "user_session:administrator", "trace_id": "trace-9"},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
        host=host,
        dispatch_arm=functools.partial(daemon.bind_dispatch_sync, config_write_op=_capture),
    )
    assert status == "completed"
    assert captured == {
        "authority": config_authority,
        "local": False,
        "actor": "user_session:administrator",
        "trace_id": "trace-9",
        "host": host,
    }


async def test_config_audit_read_op_receives_last(
    op_executor: ThreadPoolExecutor,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    captured: dict[str, object] = {}

    class _Result:
        def model_dump(self: object, **_kw: object) -> dict[str, object]:
            return {"ok": True}

    def _capture(last: int, *, local_machine: str) -> _Result:
        del local_machine
        captured["last"] = last
        return _Result()

    dispatch_pool: ConnectionPool = stub_pool()
    status, _ = await daemon.dispatch(
        "config_audit_read",
        {"last": 7},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
        host=fake_host(),
        dispatch_arm=functools.partial(daemon.bind_dispatch_sync, config_audit_read_op=_capture),
    )
    assert status == "completed"
    assert captured == {"last": 7}


async def test_config_audit_read_rejects_out_of_range_last(
    op_executor: ThreadPoolExecutor,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    dispatch_pool: ConnectionPool = stub_pool()
    status, result = await daemon.dispatch(
        "config_audit_read",
        {"last": 201},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
        host=fake_host(),
    )
    assert status == "failed"
    assert "last" in str(result["error"])


@pytest.mark.asyncio
async def test_op_arms_do_not_run_on_the_default_executor(
    op_executor: ThreadPoolExecutor,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    dispatch_pool: ConnectionPool = stub_pool()
    seen: list[str] = []

    def _note_thread(*, authority: ConfigAuthority, local_machine: str, host: Host) -> object:
        del local_machine
        seen.append(threading.current_thread().name)

        class _R:
            def model_dump(self: object, **_kw: object) -> dict[str, object]:
                return {}

        return _R()

    await daemon.dispatch(
        "config_read",
        {},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
        host=fake_host(),
        dispatch_arm=functools.partial(daemon.bind_dispatch_sync, config_read_op=_note_thread),
    )

    assert seen and seen[0].startswith("meiva-ops-arm"), f"arm ran on {seen!r}"


def test_shutting_the_pool_down_does_not_wait_for_it() -> None:
    calls: list[bool] = []

    class _FakeExecutor:
        def shutdown(self, wait: bool = True) -> None:
            calls.append(wait)

    boot.shutdown_op_pool(cast(ThreadPoolExecutor, _FakeExecutor()))
    assert calls == [False]
