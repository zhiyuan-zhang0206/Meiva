import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, cast

import anthropic
import httpx2
import pytest
from langchain_core.exceptions import ModelConnectionError

from agent.graph.hooks.compact import CompactionFailedError
from agent.graph.llm_errors import (
    FatalProviderError,
    LLMStreamCorruptedError,
    LLMStreamTruncatedError,
)
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.native_process.turn_identity import HostedServiceResources
from services.agent_runner.agent_host.runtime import StallTiming
from services.agent_runner.agent_host.stall_guard import (
    HostRestartRequiredError,
    run_invocation_with_stall_guard,
)

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
    invocations: list[asyncio.Task[object]] = []
    result: dict[str, object] = {"completed": True}

    async def invoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        task = asyncio.current_task()
        assert task is not None
        invocations.append(task)
        entered.set()
        await release.wait()
        return result

    guard = asyncio.create_task(_guarded(_Graph(invoke), ctx))
    try:
        await entered.wait()
        assert len(invocations) == 1
        actual = invocations[0]
        assert actual in service._pending
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
    invocations: list[asyncio.Task[object]] = []

    async def invoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        task = asyncio.current_task()
        assert task is not None
        invocations.append(task)
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
    assert len(invocations) == 1
    actual = invocations[0]
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


def _transient_503() -> anthropic.InternalServerError:
    response = httpx2.Response(503, request=httpx2.Request("POST", "https://audit.invalid"))
    return anthropic.InternalServerError(
        "Service unavailable",
        response=response,
        body={
            "type": "error",
            "error": {"type": "service_unavailable_error", "message": "Service unavailable"},
        },
    )


@pytest.mark.parametrize(
    "abort",
    [
        LLMStreamCorruptedError("terminal reason missing"),
        LLMStreamTruncatedError("max_tokens", stop_reason="max_tokens", output_tokens=4096),
        FatalProviderError("billing", error_class="permanent", provider="anthropic", status=402),
        CompactionFailedError("compaction failed"),
        ModelConnectionError("socket reset"),
        _transient_503(),
    ],
)
async def test_settled_invocation_aborts_propagate_without_polluting_service_failures(
    abort: Exception,
) -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    ctx = MeivaContext(identity=AgentIdentity(7, True), hosted_resources=scope)

    async def invoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise abort

    with pytest.raises(type(abort)) as propagated:
        await _guarded(_Graph(invoke), ctx)
    assert propagated.value is abort
    await asyncio.sleep(0)
    assert service.failures == []
    await service.aclose()
    assert service.joined


async def test_unknown_foreground_invocation_error_is_retained_in_service_failures() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    ctx = MeivaContext(identity=AgentIdentity(7, True), hosted_resources=scope)
    error = ValueError("unexpected foreground graph crash")

    async def invoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise error

    with pytest.raises(ValueError) as propagated:
        await _guarded(_Graph(invoke), ctx)
    assert propagated.value is error
    await asyncio.sleep(0)
    assert service.failures == [(scope, error)]
    with pytest.raises(ValueError) as joined:
        await service.aclose()
    assert joined.value is error
    assert service.joined


async def test_unwind_refusal_retains_straggler_invocation_task_for_late_failure_collection() -> (
    None
):
    service = HostedServiceResources()
    scope = await service.turn()
    ctx = MeivaContext(identity=AgentIdentity(7, True), hosted_resources=scope)
    entered, release = asyncio.Event(), asyncio.Event()
    late_error = RuntimeError("straggler crashed after unwind timeout")
    timing = StallTiming(scan_interval_s=0.01, no_progress_timeout_s=0.02, unwind_timeout_s=0.02)

    async def invoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
            raise late_error from None
        return {}

    with pytest.raises(HostRestartRequiredError):
        await run_invocation_with_stall_guard(
            cast(Any, _Graph(invoke)), 7, ctx, {}, {}, timing=timing
        )
    assert entered.is_set()
    release.set()
    await asyncio.sleep(0.02)
    assert service.failures == [(scope, late_error)]
    with pytest.raises(RuntimeError) as joined:
        await service.aclose()
    assert joined.value is late_error
    assert service.joined
