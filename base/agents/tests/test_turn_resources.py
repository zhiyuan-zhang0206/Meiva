import asyncio
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import (
    HostedServiceResources,
    HostedTurnResources,
)


def test_copied_context_keeps_original_admission_and_cleanup_scope() -> None:
    incarnation = RuntimeIncarnation(17, uuid4(), uuid4())
    resources = HostedTurnResources()
    context = MeivaContext(
        identity=AgentIdentity(17, True),
        original_incarnation=incarnation,
        hosted_resources=resources,
    )
    copied = replace(context)
    assert copied.require_original_incarnation(17) is incarnation
    assert copied.hosted_resources is resources
    with pytest.raises(RuntimeError, match="different agent"):
        copied.require_original_incarnation(18)
    with pytest.raises(RuntimeError, match="no original RuntimeIncarnation"):
        MeivaContext(identity=AgentIdentity(17, True)).require_original_incarnation(17)


async def test_late_failure_is_kept_without_cancelling_other_turns(
    loguru_records: list[dict[str, Any]],
) -> None:
    service = HostedServiceResources()
    first = await service.turn()
    failure = RuntimeError("late original completion failed")
    release, sibling_finished = asyncio.Event(), asyncio.Event()

    async def finish_sibling() -> None:
        await release.wait()
        await asyncio.sleep(0)
        sibling_finished.set()

    service.watch(finish_sibling(), name="next-turn-completion")
    service.record_failure(first, failure, name="original-completion")
    release.set()
    await sibling_finished.wait()
    assert service.failures == [(first, failure)]
    assert any("retained until service stop/join" in record["message"] for record in loguru_records)
    with pytest.raises(RuntimeError) as observed:
        await service.aclose()
    assert observed.value is failure


async def test_service_join_waits_for_the_actual_completion() -> None:
    service = HostedServiceResources()
    await service.turn()
    release = asyncio.Event()

    async def complete_original() -> None:
        await release.wait()

    service.watch(complete_original(), name="actual-completion")
    joining = asyncio.create_task(service.aclose())
    await asyncio.sleep(0)
    assert not joining.done()
    release.set()
    await joining
    with pytest.raises(RuntimeError, match="already closed"):
        await service.turn()


async def test_service_join_preserves_every_late_unknown_error() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    first, second = RuntimeError("first"), ValueError("second")
    service.record_failure(scope, first, name="first")
    service.record_failure(scope, second, name="second")
    with pytest.raises(ExceptionGroup) as observed:
        await service.aclose()
    assert observed.value.exceptions == (first, second)


async def test_uncooperative_completion_returns_bounded_without_joining() -> None:
    service = HostedServiceResources()
    await service.turn()
    release = asyncio.Event()

    async def complete() -> None:
        await release.wait()

    service.watch(complete(), name="uncooperative-original")
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(TimeoutError, match="uncooperative-original"):
        await service.aclose(deadline=started + 0.02)
    assert loop.time() - started < 0.5
    assert not service.joined
    release.set()
    await service.aclose()
    assert service.joined


async def test_join_tracks_completion_created_by_a_retained_turn_after_stop() -> None:
    service = HostedServiceResources()
    await service.turn()
    turn_release, completion_release, created = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def completion() -> None:
        await completion_release.wait()

    async def turn() -> None:
        with service.hold_turn(asyncio.current_task()):
            await turn_release.wait()
            service.watch(completion(), name="created-after-stop")
            created.set()

    root = asyncio.create_task(turn(), name="original-turn")
    await asyncio.sleep(0)
    joining = asyncio.create_task(service.aclose())
    turn_release.set()
    await created.wait()
    await root
    await asyncio.sleep(0)
    assert not joining.done()
    completion_release.set()
    await joining
    assert service.joined


async def test_unknown_and_join_timeout_preserve_both_results() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    failure = ValueError("original late unknown")
    service.record_failure(scope, failure, name="original")
    release = asyncio.Event()

    async def pending() -> None:
        await release.wait()

    service.watch(pending(), name="pending")
    with pytest.raises(ExceptionGroup) as observed:
        await service.aclose(deadline=asyncio.get_running_loop().time())
    assert observed.value.exceptions[0] is failure
    assert isinstance(observed.value.exceptions[1], TimeoutError)
    assert not service.joined
    release.set()
    with pytest.raises(ValueError) as final:
        await service.aclose()
    assert final.value is failure
    assert service.joined


async def test_repeated_join_cancel_keeps_new_completion_owned() -> None:
    service = HostedServiceResources()
    await service.turn()
    turn_release, completion_release = asyncio.Event(), asyncio.Event()

    async def completion() -> None:
        await completion_release.wait()

    async def turn() -> None:
        with service.hold_turn(asyncio.current_task()):
            await turn_release.wait()
            service.watch(completion(), name="created-during-join-cancel")
            joining.cancel()

    root = asyncio.create_task(turn(), name="original-turn")
    await asyncio.sleep(0)
    joining = asyncio.create_task(service.aclose())
    await asyncio.sleep(0)
    turn_release.set()
    await root
    await asyncio.sleep(0)
    assert not joining.done()
    completion_release.set()
    with pytest.raises(asyncio.CancelledError):
        await joining
    assert service.joined


async def test_same_exception_from_distinct_scopes_keeps_both_original_owners() -> None:
    service = HostedServiceResources()
    first, second = await service.turn(), await service.turn()
    error = ValueError("shared exception instance")
    service.record_failure(first, error, name="first")
    service.record_failure(first, error, name="first again")
    service.record_failure(second, error, name="second")
    assert service.failures == [(first, error), (second, error)]
    with pytest.raises(ExceptionGroup) as observed:
        await service.aclose()
    assert observed.value.exceptions == (error, error)
