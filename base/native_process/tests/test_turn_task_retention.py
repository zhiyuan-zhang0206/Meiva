import asyncio

import pytest

from base.log.tests.log_records import LogRecords
from base.native_process.turn_identity import HostedServiceResources, HostedTurnResources


async def test_join_retains_the_same_unfinished_child_without_cancelling_it() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    release = asyncio.Event()
    child = asyncio.create_task(release.wait(), name="original-hosted-child")
    service.retain_task(scope, child)
    try:
        with pytest.raises(TimeoutError, match="original-hosted-child"):
            await service.aclose(deadline=asyncio.get_running_loop().time())
        assert child in service._pending
        assert not child.done()
        assert not child.cancelling()
        assert not service.joined
    finally:
        release.set()
        await child
        await service.aclose()
    assert service.joined


async def test_original_error_is_visible_and_joined_without_cancelling_another_turn() -> None:
    service = HostedServiceResources()
    scope, sibling_scope = await service.turn(), await service.turn()
    error = ValueError("original hosted child failure")
    release = asyncio.Event()

    async def fail() -> None:
        raise error

    child = asyncio.create_task(fail(), name="failed-original-child")
    sibling = asyncio.create_task(release.wait(), name="unrelated-turn-child")
    service.retain_task(scope, child)
    service.retain_task(sibling_scope, sibling)
    try:
        await asyncio.wait({child}, timeout=1)
        await asyncio.sleep(0)
        assert service.failures == [(scope, error)]
        assert not sibling.done()
        assert not sibling.cancelling()
        with pytest.raises(ValueError) as observed:
            child.result()
        assert observed.value is error
    finally:
        release.set()
        await sibling
    with pytest.raises(ValueError) as joined:
        await service.aclose()
    assert joined.value is error
    assert service.joined


async def test_retained_turn_can_register_settlement_after_service_stop_started() -> None:
    service = HostedServiceResources()
    scope = await service.turn()
    turn_release, child_release, created = asyncio.Event(), asyncio.Event(), asyncio.Event()
    children: list[asyncio.Task[bool]] = []

    async def turn() -> None:
        with service.hold_turn(asyncio.current_task()):
            await turn_release.wait()
            child = asyncio.create_task(child_release.wait(), name="original-settlement")
            children.append(child)
            service.retain_task(scope, child)
            created.set()

    root = asyncio.create_task(turn())
    await asyncio.sleep(0)
    joining = asyncio.create_task(service.aclose())
    try:
        await asyncio.sleep(0)
        turn_release.set()
        await created.wait()
        await root
        await asyncio.sleep(0)
        assert len(children) == 1
        assert children[0] in service._pending
        assert not joining.done()
    finally:
        turn_release.set()
        child_release.set()
        await root
        await joining
    assert service.joined


async def test_foreign_scope_cannot_replace_original_child_ownership() -> None:
    service, foreign = HostedServiceResources(), HostedServiceResources()
    scope, other = await service.turn(), await foreign.turn()
    release = asyncio.Event()
    child = asyncio.create_task(release.wait())
    service.retain_task(scope, child)
    try:
        with pytest.raises(RuntimeError, match="original service scope"):
            foreign.retain_task(scope, child)
        with pytest.raises(RuntimeError, match="original service scope"):
            service.retain_task(other, child)
        assert child in service._pending
        assert child not in foreign._pending
    finally:
        release.set()
        await child
        await service.aclose()
        await foreign.aclose()


async def test_explicit_scope_child_is_not_joined_without_entering_the_watch_group() -> None:
    service = HostedServiceResources()
    scope = HostedTurnResources(service=service)
    release = asyncio.Event()
    child = asyncio.create_task(release.wait(), name="explicit-scope-original")
    service.retain_task(scope, child)
    try:
        assert not service.joined
        with pytest.raises(TimeoutError, match="explicit-scope-original"):
            await service.aclose(deadline=asyncio.get_running_loop().time())
        assert not service.joined
        assert child in service._pending
    finally:
        release.set()
        await child
        await service.aclose()
    assert service.joined
    with pytest.raises(RuntimeError, match="already joined"):
        service.retain_task(scope, child)


async def test_retain_task_skips_failure_recording_for_settled_exception_types(
    loguru_records: LogRecords,
) -> None:
    service = HostedServiceResources()
    settled_scope = await service.turn()

    async def fail_settled() -> None:
        raise KeyError("handled-by-caller")

    settled_child = asyncio.create_task(fail_settled(), name="settled-child")
    service.retain_task(settled_scope, settled_child, settled=(LookupError,))
    await asyncio.wait({settled_child}, timeout=1)
    await asyncio.sleep(0)
    assert service.failures == []
    assert not any(
        "retained until service stop/join" in record["message"] for record in loguru_records
    )
    await service.aclose()
    assert service.joined

    second_service = HostedServiceResources()
    unexpected_scope = await second_service.turn()
    unexpected = ValueError("unlisted-child-failure")

    async def fail_unexpected() -> None:
        raise unexpected

    unexpected_child = asyncio.create_task(fail_unexpected(), name="unexpected-child")
    second_service.retain_task(unexpected_scope, unexpected_child, settled=(LookupError,))
    await asyncio.wait({unexpected_child}, timeout=1)
    await asyncio.sleep(0)
    assert second_service.failures == [(unexpected_scope, unexpected)]
    with pytest.raises(ValueError) as joined:
        await second_service.aclose()
    assert joined.value is unexpected
    assert second_service.joined


async def test_hold_task_tracks_live_foreground_task_without_retaining_its_error() -> None:
    service, foreign = HostedServiceResources(), HostedServiceResources()
    scope, other = await service.turn(), await foreign.turn()
    release = asyncio.Event()
    expected = KeyError("foreground-caller-handles-this")

    async def fail_when_released() -> None:
        await release.wait()
        raise expected

    foreground = asyncio.create_task(fail_when_released(), name="foreground-held-task")
    service.hold_task(scope, foreground)
    try:
        with pytest.raises(RuntimeError, match="original service scope"):
            foreign.hold_task(scope, foreground)
        with pytest.raises(RuntimeError, match="original service scope"):
            service.hold_task(other, foreground)
        with pytest.raises(TimeoutError, match="foreground-held-task"):
            await service.aclose(deadline=asyncio.get_running_loop().time())
        assert foreground in service._pending
        assert not service.joined
        release.set()
        with pytest.raises(KeyError) as caught:
            await foreground
        assert caught.value is expected
        await asyncio.sleep(0)
        assert foreground not in service._pending
        assert service.failures == []
    finally:
        release.set()
        await asyncio.wait({foreground}, timeout=1)
        await service.aclose()
        await foreign.aclose()
    assert service.joined
    with pytest.raises(RuntimeError, match="already joined"):
        service.hold_task(scope, foreground)
