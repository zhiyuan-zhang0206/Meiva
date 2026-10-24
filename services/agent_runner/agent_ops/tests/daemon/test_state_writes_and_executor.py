# meiva:note
# State writes are serialized inside the daemon, and operations run on the daemon's own pool.

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from services.agent_runner.agent_ops import boot
from services.agent_runner.agent_ops.tests.ops_support import Ops, config_write, held_env_lock


async def _until(condition: Callable[[], bool], *, timeout_s: float = 10.0) -> None:
    for _ in range(int(timeout_s / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never held")


# meiva:note
# Two state writes in one daemon must not interleave: `.env` is rewritten read-modify-write,
# and a later writer would carry the earlier one's snapshot over its keys. A config write held
# inside its rewrite keeps an inventory write, which would otherwise finish at once, waiting.
async def test_a_state_write_waits_for_the_write_in_progress(ops: Ops) -> None:
    with held_env_lock():
        config = asyncio.create_task(ops.dispatch("config_write", config_write("serialized")))
        await _until(lambda: "config_write" in ops.active_ops)
        inventory = asyncio.create_task(
            ops.dispatch("inventory_write", {"plugins": {}, "mcp_servers": {}})
        )
        await asyncio.sleep(0.3)

        assert not inventory.done(), "two state writes ran concurrently — .env can lose fields"

    assert (await config)[0] == "completed"
    assert (await inventory)[0] == "completed"


async def test_operations_run_on_the_daemons_own_pool_not_the_default_executor(ops: Ops) -> None:
    with held_env_lock():
        stuck = asyncio.create_task(ops.dispatch("config_write", config_write("own-pool")))
        await _until(lambda: "config_write" in ops.active_ops)
        await _until(lambda: any(t.name.startswith("meiva-ops-arm") for t in threading.enumerate()))

        default_pool_threads = [
            t.name for t in threading.enumerate() if t.name.startswith("asyncio_")
        ]

    await stuck
    assert default_pool_threads == []


def test_shutting_the_pool_down_does_not_wait_for_a_running_operation() -> None:
    pool = ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    started = threading.Event()

    def blocked() -> None:
        started.set()
        release.wait(timeout=30)

    future = pool.submit(blocked)
    assert started.wait(10)
    try:
        began = time.monotonic()
        boot.shutdown_op_pool(pool)

        assert time.monotonic() - began < 5.0
        assert not future.done()
    finally:
        release.set()
        future.result(timeout=10)
