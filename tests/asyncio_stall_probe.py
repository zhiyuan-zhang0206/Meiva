from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

import pytest
from _pytest.faulthandler import fault_handler_stderr_fd_key

DUMP_MARGIN_SECONDS = 2.0

_MIN_DELAY_SECONDS = 0.05


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
# The event-loop factory of one test under a faulthandler deadline: a loop that schedules the chain
# dump `DUMP_MARGIN_SECONDS` before the deadline, on the loop itself. pytest-asyncio builds the
# loops of its tests through this factory (`pytest_asyncio_loop_factories`), so nothing in asyncio
# is replaced; a loop a test builds for itself (`asyncio.run`) is not covered. Without a deadline
# the factory is the plain `asyncio.new_event_loop`.
def probing_loop_factory(
    nodeid: str, timeout: float, stderr_fd: int
) -> Callable[[], asyncio.AbstractEventLoop]:
    if timeout <= 0.0:
        return asyncio.new_event_loop

    def factory() -> asyncio.AbstractEventLoop:
        loop = asyncio.new_event_loop()
        delay = max(timeout - DUMP_MARGIN_SECONDS, _MIN_DELAY_SECONDS)
        loop.call_later(delay, _dump_pending_chains, stderr_fd, nodeid, timeout)
        return loop

    return factory


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
    timeout = faulthandler_timeout(config)
    stderr_fd = config.stash[fault_handler_stderr_fd_key] if timeout > 0.0 else -1
    return {"stall-probe": probing_loop_factory(item.nodeid, timeout, stderr_fd)}
