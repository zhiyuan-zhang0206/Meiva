import asyncio

import pytest

from agent.graph.interrupt import (
    InterruptEvent,
    ModelInterruptedError,
    interrupt_halt,
    interruptible_model,
)
from base.agents.messages.inbound import InterruptReason


async def test_original_provider_failure_reaches_caller_without_exception_group() -> None:
    original = ValueError("provider failed")

    async def provider() -> str:
        raise original

    with pytest.raises(ValueError) as caught:
        await interruptible_model(provider(), asyncio.Event())
    assert caught.value is original


async def test_interrupt_waits_for_provider_cleanup_and_exposes_unknown_cleanup_error() -> None:
    event, entered, unwound = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = RuntimeError("provider cleanup failed")

    async def provider() -> str:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            unwound.set()
            raise original

    async def interrupt() -> None:
        await entered.wait()
        event.set()

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(interrupt())
        with pytest.raises(RuntimeError) as caught:
            await interruptible_model(provider(), event)
    assert caught.value is original
    assert unwound.is_set()


async def test_interrupt_returns_only_after_provider_unwinds() -> None:
    event, entered, unwound = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def provider() -> str:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            unwound.set()
        raise AssertionError("unreachable")

    async def interrupt() -> None:
        await entered.wait()
        event.set()

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(interrupt())
        with pytest.raises(ModelInterruptedError):
            await interruptible_model(provider(), event)
    assert unwound.is_set()


async def test_provider_value_that_is_an_exception_is_still_a_value() -> None:
    value = ValueError("a legitimate provider value")

    async def provider() -> ValueError:
        return value

    assert await interruptible_model(provider(), asyncio.Event()) is value


async def test_model_completion_and_cancel_same_tick_discards_result() -> None:
    interrupted = asyncio.Event()
    interrupted.set()

    async def completed() -> str:
        return "a complete but superseded summary"

    with pytest.raises(ModelInterruptedError):
        await interruptible_model(completed(), interrupted)


@pytest.mark.parametrize(
    ("reason", "update"),
    [
        (InterruptReason.USER, {"halted": True}),
        (InterruptReason.SYSTEM, {"halted": True}),
        (InterruptReason.RESTART, {"halted": True, "interrupted_by_restart": True}),
    ],
)
def test_an_interrupted_node_halts_and_only_a_restart_marks_the_turn_unfinished(
    reason: InterruptReason, update: dict[str, bool]
) -> None:
    assert interrupt_halt(reason) == update


async def test_the_interrupt_error_carries_the_reason_of_the_event_that_ended_the_call() -> None:
    event = InterruptEvent()
    event.set(InterruptReason.RESTART)

    async def never() -> None:
        await asyncio.Event().wait()

    with pytest.raises(ModelInterruptedError) as raised:
        await interruptible_model(never(), event)
    assert raised.value.reason is InterruptReason.RESTART
