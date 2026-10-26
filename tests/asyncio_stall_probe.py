from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any

import pytest
from _pytest.faulthandler import fault_handler_stderr_fd_key

DUMP_MARGIN_SECONDS = 2.0

_MIN_DELAY_SECONDS = 0.05


# meiva:note
# The test whose protocol is running: its runtime node id (a parametrized test carries its
# `[param]`, which pytest-asyncio's collection-time hook cannot see) and when its protocol
# started, which is when pytest arms the faulthandler deadline.
@dataclass
class RunningTest:
    nodeid: str = ""
    started: float = 0.0


_running_key = pytest.StashKey[RunningTest]()


def faulthandler_timeout(config: pytest.Config) -> float:
    try:
        return float(config.getini("faulthandler_timeout") or 0.0)
    except ValueError:
        return 0.0


def awaiter_chain_lines(task: asyncio.Task[Any]) -> list[str]:
    lines: list[str] = []
    awaitable: object = task.get_coro()
    while awaitable is not None:
        frame: Any = getattr(awaitable, "cr_frame", None) or getattr(awaitable, "ag_frame", None)
        if frame is not None:
            code = frame.f_code
            lines.append(f'  File "{code.co_filename}", line {frame.f_lineno}, in {code.co_name}')
        awaitable = getattr(awaitable, "cr_await", None) or getattr(awaitable, "ag_await", None)
    return lines


def _dump_pending_chains(stderr_fd: int, nodeid: str, timeout: float) -> None:
    tasks = sorted(asyncio.all_tasks(), key=lambda task: task.get_name())
    lines = [
        f"=== asyncio stall probe: {nodeid} ({len(tasks)} pending asyncio task(s), "
        f"dumping {DUMP_MARGIN_SECONDS:g}s before the {timeout:g}s faulthandler dump) ==="
    ]
    for task in tasks:
        lines.append(f"--- task {task.get_name()} ---")
        lines.extend(awaiter_chain_lines(task) or ["  <no frames captured>"])
    data = ("\n".join(lines) + "\n").encode("utf-8", errors="backslashreplace")
    while data:
        data = data[os.write(stderr_fd, data) :]


# meiva:note
# The event-loop factory of the tests under a faulthandler deadline: a loop that schedules the chain
# dump `DUMP_MARGIN_SECONDS` before the deadline, on the loop itself. The deadline runs from the start
# of the test's protocol, so the time `running` shows has already passed (fixture setup before the
# loop exists) is taken off the delay. pytest-asyncio builds the loops of its tests through this
# factory (`pytest_asyncio_loop_factories`), so nothing in asyncio is replaced; a loop a test builds
# for itself (`asyncio.run`, `new_event_loop`, a `TestClient` portal) is not covered. Without a
# deadline the factory is the plain `asyncio.new_event_loop`.
def probing_loop_factory(
    running: RunningTest, timeout: float, stderr_fd: int, monotonic: Callable[[], float]
) -> Callable[[], asyncio.AbstractEventLoop]:
    if timeout <= 0.0:
        return asyncio.new_event_loop

    def factory() -> asyncio.AbstractEventLoop:
        loop = asyncio.new_event_loop()
        delay = max(
            timeout - DUMP_MARGIN_SECONDS - (monotonic() - running.started), _MIN_DELAY_SECONDS
        )
        loop.call_later(delay, _dump_pending_chains, stderr_fd, running.nodeid, timeout)
        return loop

    return factory


def pytest_asyncio_loop_factories(
    config: pytest.Config,
) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
    timeout = faulthandler_timeout(config)
    stderr_fd = config.stash[fault_handler_stderr_fd_key] if timeout > 0.0 else -1
    running = config.stash.setdefault(_running_key, RunningTest())
    return {"stall-probe": probing_loop_factory(running, timeout, stderr_fd, time.monotonic)}


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
    running = item.config.stash.setdefault(_running_key, RunningTest())
    running.nodeid = item.nodeid
    running.started = time.monotonic()
    return (yield)
