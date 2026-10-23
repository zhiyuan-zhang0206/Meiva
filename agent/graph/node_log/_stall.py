import asyncio
import contextlib
import faulthandler
import sys
from collections.abc import Collection, Generator
from typing import Any, TextIO

from ._plan import StallConfig, stall_probe


# meiva:note
# The chain of awaited frames from a task's outermost coroutine down to the leaf it is suspended
# on. `Task.get_stack()` stops at the outermost suspension point, which cannot name the blocked
# call.
# meiva:link agent/graph/docs/node-lifecycle.meiva.okf.md
def awaiter_chain_lines(task: asyncio.Task[Any]) -> list[str]:
    lines: list[str] = []
    awaitable: Any = task.get_coro()
    while awaitable is not None:
        frame = getattr(awaitable, "cr_frame", None) or getattr(awaitable, "ag_frame", None)
        if frame is not None:
            code = frame.f_code
            lines.append(f'  File "{code.co_filename}", line {frame.f_lineno}, in {code.co_name}')
        awaitable = getattr(awaitable, "cr_await", None) or getattr(awaitable, "ag_await", None)
    return lines


def render_task_dump(tasks: Collection[asyncio.Task[Any]]) -> str:
    out = [f"=== asyncio task await chains (stall probe, {len(tasks)} tasks) ==="]
    for task in tasks:
        out.append(f"--- task {task.get_name()} ---")
        out.extend(awaiter_chain_lines(task))
    return "\n".join(out) + "\n"


def dump_async_tasks(stderr: TextIO) -> None:
    stderr.write(render_task_dump(asyncio.all_tasks()))
    stderr.flush()


# meiva:note
# The interpreter's original stderr, never the swappable `sys.stderr`: an exec stream redirect
# that leaked leaves `sys.stderr` on a buffer without a descriptor, and faulthandler needs a real
# one. A process without a usable descriptor skips the dump instead of crashing the agent the
# probe exists to observe.
def _real_stderr() -> TextIO | None:
    stderr = sys.__stderr__
    if stderr is None:
        return None
    try:
        stderr.fileno()
    except (OSError, ValueError):
        return None
    return stderr


# meiva:note
# Arms the stall probe around a node body: a loop timer dumps every asyncio task's await chain at
# the threshold and faulthandler dumps every thread's frames one stagger later, both disarmed on
# the way out. faulthandler's timer is process-global, which is why nothing but this guard may arm
# it.
# meiva:link agent/graph/docs/node-lifecycle.meiva.okf.md
@contextlib.contextmanager
def stall_guard(node_name: str, config: StallConfig) -> Generator[None]:
    probe = stall_probe(node_name, config)
    stderr = _real_stderr()
    if probe is None or stderr is None:
        yield
        return
    faulthandler.dump_traceback_later(probe.thread_dump_after_s, repeat=False, file=stderr)
    timer = asyncio.get_running_loop().call_later(probe.task_dump_after_s, dump_async_tasks, stderr)
    try:
        yield
    finally:
        timer.cancel()
        faulthandler.cancel_dump_traceback_later()
