import asyncio
import contextlib
import json
import multiprocessing
import os
import shutil
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio.client import PubSub

from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.events.live.bus import EventBus, EventBusConfig
from base.events.live.projection import ToUserChanged
from services.agent_runner.agent_ops.to_user_watch import watch_to_user


# meiva:note
# Forward live events from the Redis pub/sub channel into an in-memory queue so assertions stay non-blocking.
async def _pump_events(subscriber: PubSub, events: asyncio.Queue[ToUserChanged]) -> None:
    while True:
        message: dict[str, Any] | None = await subscriber.get_message(
            ignore_subscribe_messages=True, timeout=1.0
        )
        if message:
            payload = message["data"]
            assert isinstance(payload, (str, bytes))
            events.put_nowait(ToUserChanged.model_validate(json.loads(payload)))


@asynccontextmanager
async def subscribed(bus: EventBus) -> AsyncGenerator[asyncio.Queue[ToUserChanged]]:
    events: asyncio.Queue[ToUserChanged] = asyncio.Queue()
    async with (
        bus.open_async_redis() as client,
        PubSub(client.connection_pool) as subscriber,
    ):
        await subscriber.subscribe(bus.channel)
        await subscriber.get_message(timeout=1)
        pump = asyncio.create_task(_pump_events(subscriber, events))
        try:
            yield events
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump


async def next_event(events: asyncio.Queue[ToUserChanged]) -> ToUserChanged:
    return await asyncio.wait_for(events.get(), timeout=5)


async def recorded_event(events: asyncio.Queue[ToUserChanged]) -> int:
    return (await next_event(events)).agent_id


@pytest.mark.asyncio
async def test_changes_atomic_replacement_and_new_workspace(
    tmp_path: Path, event_bus: EventBus
) -> None:
    workspace = tmp_path / "7"
    workspace.mkdir()
    async with subscribed(event_bus) as events, asyncio.TaskGroup() as group:
        watcher = group.create_task(watch_to_user(tmp_path, event_bus))
        try:
            await asyncio.sleep(0.2)
            (workspace / "to_user.md").write_text("first")
            assert (await next_event(events)).agent_id == 7
            staged = workspace / ".next"
            staged.write_text("second")
            staged.replace(workspace / "to_user.md")
            assert (await next_event(events)).agent_id == 7
            (tmp_path / "8").mkdir()
            (tmp_path / "8" / "to_user.md").write_text("new agent")
            assert (await next_event(events)).agent_id == 8
            await asyncio.sleep(0.2)
            (tmp_path / "8" / "to_user.md").write_text("updated")
            assert (await next_event(events)).agent_id == 8
        finally:
            watcher.cancel()


@pytest.mark.asyncio
async def test_shallow_snapshots_coalesce_and_detect_preserved_mtime(
    tmp_path: Path, event_bus: EventBus
) -> None:
    workspace = tmp_path / "7"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (tmp_path / "8").symlink_to(external, target_is_directory=True)
    async with subscribed(event_bus) as events, asyncio.TaskGroup() as group:
        watcher = group.create_task(watch_to_user(tmp_path, event_bus))
        try:
            await asyncio.sleep(0.7)
            assert events.empty()
            (workspace / "repository").mkdir()
            (workspace / "repository" / "to_user.md").write_text("nested")
            (workspace / "unrelated").write_text("other")
            (external / "to_user.md").write_text("symlink workspace")
            await asyncio.sleep(0.7)
            assert events.empty()
            markdown = workspace / "to_user.md"
            markdown.write_text("first")
            assert await recorded_event(events) == 7
            await asyncio.sleep(0.7)
            assert events.empty()
            before = markdown.stat()
            markdown.write_text("other")
            os.utime(markdown, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert await recorded_event(events) == 7
            staged = workspace / ".next"
            staged.write_text("third")
            os.utime(staged, ns=(before.st_atime_ns, before.st_mtime_ns))
            staged.replace(markdown)
            assert await recorded_event(events) == 7
            markdown.unlink()
            assert await recorded_event(events) == 7
            shutil.rmtree(workspace)
            assert await recorded_event(events) == 7
            (tmp_path / "9").mkdir()
            assert await recorded_event(events) == 9
            (tmp_path / "9").rmdir()
            assert await recorded_event(events) == 9
        finally:
            watcher.cancel()


@pytest.mark.asyncio
async def test_target_symlink_watches_link_not_destination(
    tmp_path: Path, event_bus: EventBus
) -> None:
    workspace = tmp_path / "7"
    workspace.mkdir()
    external = tmp_path / "external"
    external.write_text("first")
    target = workspace / "to_user.md"
    target.symlink_to(external)
    async with subscribed(event_bus) as events, asyncio.TaskGroup() as group:
        watcher = group.create_task(watch_to_user(tmp_path, event_bus))
        try:
            await asyncio.sleep(0.7)
            external.write_text("second")
            await asyncio.sleep(0.7)
            assert events.empty()
            target.unlink()
            target.symlink_to(tmp_path / "missing")
            assert await recorded_event(events) == 7
        finally:
            watcher.cancel()


@pytest.mark.asyncio
async def test_removed_watch_root_fails_instead_of_hiding_io_errors(
    tmp_path: Path, event_bus: EventBus
) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    with pytest.raises(ExceptionGroup) as caught:
        async with asyncio.TaskGroup() as group:
            group.create_task(watch_to_user(root, event_bus))
            await asyncio.sleep(0.7)
            root.rmdir()
    assert len(caught.value.exceptions) == 1
    assert isinstance(caught.value.exceptions[0], FileNotFoundError)


@pytest.mark.asyncio
async def test_unreadable_workspace_propagates_permission_error(
    tmp_path: Path, event_bus: EventBus
) -> None:
    if os.geteuid() == 0:
        pytest.skip("Root can read mode-000 directories")
    workspace = tmp_path / "7"
    workspace.mkdir()
    workspace.chmod(0)
    try:
        with pytest.raises(PermissionError):
            await watch_to_user(tmp_path, event_bus)
    finally:
        workspace.chmod(0o700)


@pytest.mark.asyncio
async def test_workspace_replaced_by_file_during_scan_is_a_removal(
    tmp_path: Path, event_bus: EventBus
) -> None:
    class ReplacedWorkspaceRoot(Path):
        def __truediv__(self, key: str | os.PathLike[str]) -> Path:

            path = Path(super().__truediv__(key))
            path.rmdir()
            path.write_text("workspace replaced")
            return path

    (tmp_path / "7").mkdir()
    async with subscribed(event_bus) as subscriber, asyncio.TaskGroup() as group:
        watcher = group.create_task(watch_to_user(ReplacedWorkspaceRoot(tmp_path), event_bus))
        try:
            assert await recorded_event(subscriber) == 7
            assert (tmp_path / "7").is_file()
        finally:
            watcher.cancel()


async def scale_probe(root: Path, bus_config: EventBusConfig) -> dict[str, float | int]:

    for index in range(3688):
        workspace = root / str(index)
        workspace.mkdir()
        (workspace / "to_user.md").write_text("existing markdown")
    bus = EventBus(bus_config, rng=seeded_rng(), timers=SYSTEM_TIMERS)
    loop = asyncio.get_running_loop()
    gaps: list[float] = []
    started = previous = loop.time()
    cpu_started = time.process_time()
    async with subscribed(bus) as events, asyncio.TaskGroup() as group:
        watcher = group.create_task(watch_to_user(root, bus))
        try:
            while loop.time() - started < 3:
                await asyncio.sleep(0.02)
                now = loop.time()
                gaps.append(now - previous)
                previous = now
                if now - started >= 0.7 and not (root / "4000").exists():
                    assert events.empty()
                    (root / "4000").mkdir()
                if now - started >= 1.7 and not (root / "4001").exists():
                    (root / "4001").mkdir()
            assert await recorded_event(events) == 4000
            assert await recorded_event(events) == 4001
        finally:
            watcher.cancel()
    return {
        "roots": 3688,
        "max_loop_gap": max(gaps),
        "elapsed": loop.time() - started,
        "cpu_seconds": time.process_time() - cpu_started,
    }


def run_scale_probe(root: Path, bus_config: EventBusConfig, result: Connection) -> None:
    result.send(asyncio.run(scale_probe(root, bus_config)))
    result.close()


def test_production_scale_keeps_loop_responsive_and_stops(
    tmp_path: Path, event_bus: EventBus
) -> None:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=run_scale_probe, args=(tmp_path, event_bus._config, sender))
    worker.start()
    sender.close()
    try:
        assert receiver.poll(20), "The isolated watcher probe did not finish within its deadline"
        sample = receiver.recv()
        worker.join(2)
        assert worker.exitcode == 0
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(2)
        receiver.close()
        worker.close()
    assert sample["roots"] == 3688
    assert sample["max_loop_gap"] < 0.5, sample
    assert sample["elapsed"] < 5, sample
