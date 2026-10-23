import asyncio
import contextlib
import os
import sys
from pathlib import Path

import pytest

from agent.graph.node_log._stall import render_task_dump
from base.host.proc import run_bounded

_CHILD = Path(__file__).with_name("stall_child.py")
_TASK_DUMP = "asyncio task await chains (stall probe"
_THREAD_DUMP = "most recent call first"


def _run_child(mode: str) -> str:
    result = run_bounded(
        [sys.executable, str(_CHILD), mode],
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stderr


def test_a_stalled_body_gets_the_task_dump_then_the_thread_dump() -> None:
    stderr = _run_child("stalled")
    assert _TASK_DUMP in stderr
    assert _THREAD_DUMP in stderr
    assert stderr.index(_TASK_DUMP) < stderr.index(_THREAD_DUMP)


def test_a_leaked_stderr_redirect_cannot_take_the_guard_down_and_the_dump_reaches_the_real_fd() -> (
    None
):
    stderr = _run_child("leaked-redirect")
    assert _TASK_DUMP in stderr
    assert _THREAD_DUMP in stderr


@pytest.mark.parametrize(
    "mode", ["exempt-node", "disabled", "fast-body-then-quiet", "failed-body-then-quiet"]
)
def test_a_guard_that_is_not_armed_or_already_disarmed_dumps_nothing(mode: str) -> None:
    assert _run_child(mode) == ""


async def test_the_task_dump_names_the_full_await_chain_down_to_the_leaf() -> None:
    async def inner_leaf_await() -> None:
        await asyncio.sleep(30)

    async def outer_wrapper() -> None:
        await inner_leaf_await()

    task = asyncio.create_task(outer_wrapper(), name="stall-probe-target")
    await asyncio.sleep(0)
    try:
        dump = render_task_dump([task])
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert _TASK_DUMP in dump
    assert "--- task stall-probe-target ---" in dump
    assert "in outer_wrapper" in dump
    assert "in inner_leaf_await" in dump
    assert "in sleep" in dump
