import asyncio

import pytest

from services.agent_runner.agent_host.tests.runtime.host_support import Build, Row
from services.agent_runner.agent_host.tests.runtime.host_support import host_plugin as host_plugin
from services.agent_runner.agent_host.tests.runtime.host_support import wired as wired


async def test_business_error_remains_primary_and_is_joined_at_stop(
    wired: Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, _, _ = wired({7: Row()})
    primary = ValueError("original hosted business failure")

    async def work(_agent: int, **_kwargs: object) -> None:
        raise primary

    monkeypatch.setattr(host, "_run_turn", work)
    with pytest.raises(ValueError) as observed:
        await host.run_turn(7)
    assert observed.value is primary
    with pytest.raises(ValueError) as joined:
        await host._resource_service.aclose()
    assert joined.value is primary


async def test_root_cancellation_cancels_the_work_and_joins_it(
    wired: Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, _, _ = wired({7: Row()})
    work_entered = asyncio.Event()
    actual_work: asyncio.Task[object] | None = None

    async def work(_agent: int, **_kwargs: object) -> None:
        nonlocal actual_work
        actual_work = asyncio.current_task()
        work_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(host, "_run_turn", work)
    root = asyncio.create_task(host.run_turn(7))
    try:
        await work_entered.wait()
        assert actual_work is not None and actual_work in host._resource_service._pending
        root.cancel()
        with pytest.raises(asyncio.CancelledError):
            await root
        assert actual_work.cancelled()
        await host._resource_service.aclose()
        assert host.resources_joined
    finally:
        root.cancel()
