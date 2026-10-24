import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, cast

import pytest

from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.native_process.turn_identity import HostedServiceResources
from services.agent_runner.agent_host.runtime import StallTiming
from services.agent_runner.agent_host.stall_guard import run_invocation_with_stall_guard

# meiva:note
# A guard that sees a turn make progress, so only the unwind bound is exercised here.
TIMING = StallTiming(scan_interval_s=0.01, no_progress_timeout_s=60.0, unwind_timeout_s=1.0)


# meiva:note
# A graph whose `ainvoke` is the coroutine function the test gives it.
class _Graph:
    def __init__(self, ainvoke: Callable[..., Awaitable[dict[str, object]]]) -> None:
        self.ainvoke = ainvoke


def _guarded(graph: _Graph, ctx: MeivaContext) -> Coroutine[Any, Any, dict[str, object]]:
    return run_invocation_with_stall_guard(cast(Any, graph), 7, ctx, {}, {}, timing=TIMING)


async def test_guard_registers_the_actual_invocation_and_preserves_its_result() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    ctx = MeivaContext(identity=AgentIdentity(7, True), hosted_resources=scope)
    entered, release = asyncio.Event(), asyncio.Event()
    actual: asyncio.Task[object] | None = None
    result: dict[str, object] = {"completed": True}

    async def invoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal actual
        actual = asyncio.current_task()
        entered.set()
        await release.wait()
        return result

    guard = asyncio.create_task(_guarded(_Graph(invoke), ctx))
    try:
        await entered.wait()
        assert actual is not None and actual in service._pending
        release.set()
        assert await guard is result
        assert actual.done()
        assert actual.result() is result
    finally:
        release.set()
        await guard
        await service.aclose()


async def test_external_cancel_retains_the_same_unknown_invocation_error() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    ctx = MeivaContext(identity=AgentIdentity(7, True), hosted_resources=scope)
    entered = asyncio.Event()
    error = ValueError("original invocation cancellation failure")
    actual: asyncio.Task[object] | None = None

    async def invoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal actual
        actual = asyncio.current_task()
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise error from None
        return {}

    guard = asyncio.create_task(_guarded(_Graph(invoke), ctx))
    await entered.wait()
    guard.cancel()
    with pytest.raises(ValueError) as propagated:
        await guard
    assert propagated.value is error
    await asyncio.sleep(0)
    assert actual is not None
    with pytest.raises(ValueError) as original:
        actual.result()
    assert original.value is error
    assert service.failures == [(scope, error)]
    with pytest.raises(ValueError) as joined:
        await service.aclose()
    assert joined.value is error


async def test_guard_requires_an_explicit_service_before_starting_invocation() -> None:
    async def invoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("the invocation must not start without its resource scope")

    with pytest.raises(RuntimeError, match="original resource scope"):
        await _guarded(_Graph(invoke), MeivaContext())
