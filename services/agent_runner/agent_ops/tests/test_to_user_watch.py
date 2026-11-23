import asyncio
import contextlib
import json
import multiprocessing
import os
import shutil
import sys
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import TypeAdapter
from redis.asyncio.client import PubSub

from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.events.live.bus import EventBus, EventBusConfig
from base.events.live.projection import OngoingChanged, ToUserChanged, WorkspacePanelsResync
from services.agent_runner.agent_ops.to_user_watch import watch_to_user

type PanelEvent = ToUserChanged | OngoingChanged | WorkspacePanelsResync
PANEL_EVENT = TypeAdapter[PanelEvent](PanelEvent)


# meiva:note
# Forward live events from the Redis pub/sub channel into an in-memory queue so assertions stay non-blocking.
async def _pump_events(subscriber: PubSub, events: asyncio.Queue[PanelEvent]) -> None:
    while True:
        message = cast(
            "dict[str, Any] | None",
            await subscriber.get_message(ignore_subscribe_messages=True, timeout=1.0),
        )
        if message:
            payload = message["data"]
            assert isinstance(payload, (str, bytes))
            events.put_nowait(PANEL_EVENT.validate_python(json.loads(payload)))


@asynccontextmanager
async def subscribed(bus: EventBus) -> AsyncGenerator[asyncio.Queue[PanelEvent]]:
    events: asyncio.Queue[PanelEvent] = asyncio.Queue()
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


async def next_event(events: asyncio.Queue[PanelEvent]) -> PanelEvent:
    return await asyncio.wait_for(events.get(), timeout=5)


async def recorded_event(events: asyncio.Queue[PanelEvent]) -> int:
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
            assert await next_event(events) == WorkspacePanelsResync()
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
async def test_ongoing_edits_publish_only_their_own_event(
    tmp_path: Path, event_bus: EventBus
) -> None:
    workspace = tmp_path / "7"
    workspace.mkdir()
    ongoing = workspace / "ongoing.md"
    async with subscribed(event_bus) as events, asyncio.TaskGroup() as group:
        watcher = group.create_task(watch_to_user(tmp_path, event_bus))
        try:
            assert await next_event(events) == WorkspacePanelsResync()
            ongoing.write_text("## Doing")
            assert await next_event(events) == OngoingChanged(agent_id=7)
            ongoing.write_text("## Waiting")
            assert await next_event(events) == OngoingChanged(agent_id=7)
            ongoing.unlink()
            assert await next_event(events) == OngoingChanged(agent_id=7)
            assert not ongoing.exists()
        finally:
            watcher.cancel()


@pytest.mark.asyncio
async def test_runner_restart_resyncs_after_an_edit_and_removal_while_stopped(
    tmp_path: Path, event_bus: EventBus
) -> None:
    workspace = tmp_path / "7"
    workspace.mkdir()
    shared = workspace / "to_user.md"
    ongoing = workspace / "ongoing.md"
    shared.write_text("Old shared context")
    ongoing.write_text("Old ongoing work")
    async with subscribed(event_bus) as events:
        async with asyncio.TaskGroup() as group:
            watcher = group.create_task(watch_to_user(tmp_path, event_bus))
            try:
                assert await next_event(events) == WorkspacePanelsResync()
            finally:
                watcher.cancel()
        shared.write_text("Updated while stopped")
        ongoing.unlink()
        async with asyncio.TaskGroup() as group:
            watcher = group.create_task(watch_to_user(tmp_path, event_bus))
            try:
                assert await next_event(events) == WorkspacePanelsResync()
                assert events.empty()
            finally:
                watcher.cancel()


@pytest.mark.asyncio
async def test_resync_retries_once_after_a_transient_failure(tmp_path: Path) -> None:
    published: asyncio.Queue[PanelEvent] = asyncio.Queue()
    first_failed = asyncio.Event()

    class RecoveringPublisher:
        attempts = 0

        async def publish_best_effort(self, payload: str, *, context: str) -> int | None:
            event = PANEL_EVENT.validate_json(payload)
            if isinstance(event, WorkspacePanelsResync):
                assert context == "workspace_panels_resync"
                self.attempts += 1
                if self.attempts == 1:
                    first_failed.set()
                    return None
            published.put_nowait(event)
            return 0

    bus = RecoveringPublisher()
    workspace = tmp_path / "7"
    workspace.mkdir()
    async with asyncio.TaskGroup() as group:
        watcher = group.create_task(watch_to_user(tmp_path, bus))
        try:
            await asyncio.wait_for(first_failed.wait(), timeout=5)
            _ = (workspace / "ongoing.md").write_text("Changed while resync was pending")
            events = [await next_event(published), await next_event(published)]
            assert {event.role: event.agent_id for event in events} == {
                "ongoing_changed": 7,
                "workspace_panels_resync": 0,
            }
            for content in ("first", "second", "third"):
                _ = (workspace / "ongoing.md").write_text(content)
                assert await next_event(published) == OngoingChanged(agent_id=7)
            assert bus.attempts == 2
            assert published.empty()
        finally:
            _ = watcher.cancel()


@pytest.mark.asyncio
async def test_in_flight_resync_leaves_scans_running_and_is_joined_on_exit(tmp_path: Path) -> None:
    published: asyncio.Queue[PanelEvent] = asyncio.Queue()
    started = asyncio.Event()
    cancelling = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleaned_up = asyncio.Event()

    class BlockingPublisher:
        attempts = 0

        async def publish_best_effort(self, payload: str, *, context: str) -> int | None:
            if context == "workspace_panels_resync":
                self.attempts += 1
                started.set()
                try:
                    await asyncio.Future[None]()
                finally:
                    cancelling.set()
                    await release_cleanup.wait()
                    cleaned_up.set()
            published.put_nowait(PANEL_EVENT.validate_json(payload))
            return 0

    bus = BlockingPublisher()
    workspace = tmp_path / "7"
    workspace.mkdir()
    ongoing = workspace / "ongoing.md"
    async with asyncio.TaskGroup() as group:
        watcher = group.create_task(watch_to_user(tmp_path, bus))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            for content in ("created", "rewritten"):
                _ = ongoing.write_text(content)
                assert await next_event(published) == OngoingChanged(agent_id=7)
            ongoing.unlink()
            assert await next_event(published) == OngoingChanged(agent_id=7)
            assert bus.attempts == 1
            _ = watcher.cancel()
            await asyncio.wait_for(cancelling.wait(), timeout=5)
            assert not watcher.done()
        finally:
            release_cleanup.set()
    assert cleaned_up.is_set()
    assert watcher.cancelled()


@pytest.mark.asyncio
async def test_ignores_noncanonical_workspace_aliases(tmp_path: Path, event_bus: EventBus) -> None:
    canonical = tmp_path / "7"
    canonical.mkdir()
    alias = tmp_path / "07"
    alias.mkdir()
    (alias / "to_user.md").write_text("Alias shared context")
    (alias / "ongoing.md").write_text("Alias ongoing work")
    async with subscribed(event_bus) as events, asyncio.TaskGroup() as group:
        watcher = group.create_task(watch_to_user(tmp_path, event_bus))
        try:
            assert await next_event(events) == WorkspacePanelsResync()
            (canonical / "to_user.md").write_text("Canonical shared context")
            (canonical / "ongoing.md").write_text("Canonical ongoing work")
            assert await next_event(events) == ToUserChanged(agent_id=7)
            assert await next_event(events) == OngoingChanged(agent_id=7)
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
            assert await next_event(events) == WorkspacePanelsResync()
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
            assert await next_event(events) == WorkspacePanelsResync()
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
    with pytest.RaisesGroup(FileNotFoundError, flatten_subgroups=True):
        async with asyncio.TaskGroup() as group:
            group.create_task(watch_to_user(root, event_bus))
            await asyncio.sleep(0.7)
            root.rmdir()


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
        with pytest.RaisesGroup(PermissionError):
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
            assert await next_event(subscriber) == WorkspacePanelsResync()
            assert await recorded_event(subscriber) == 7
            assert (tmp_path / "7").is_file()
        finally:
            watcher.cancel()


# meiva:note
# Which thread each directory scan of `root` ran on, read from the interpreter's own audit event so
# the production module is not patched. The probe runs in a spawned child that exits right after,
# which is why a hook that can never be removed is acceptable here.
def _record_scan_threads(root: Path) -> list[int]:
    scanned_on: list[int] = []

    def hook(event: str, args: tuple[Any, ...]) -> None:
        scanned = args[0] if event == "os.scandir" else None
        if isinstance(scanned, (str, os.PathLike)) and os.fspath(scanned) == os.fspath(root):
            scanned_on.append(threading.get_ident())

    sys.addaudithook(hook)
    return scanned_on


# meiva:note
# The property under test is that the watcher never scans 3688 workspaces on the event loop's own
# thread, so the probe counts where the scans ran instead of timing how long the loop went quiet: a
# wall-clock gap also records a stall of the whole process (a loaded runner paused this child for
# 0.6 s while it used the same 0.45 s of CPU as an idle run), which says nothing about the watcher.
async def scale_probe(root: Path, bus_config: EventBusConfig) -> dict[str, int]:

    for index in range(3688):
        workspace = root / str(index)
        workspace.mkdir()
        (workspace / "to_user.md").write_text("existing markdown")
    scanned_on = _record_scan_threads(root)
    loop_thread = threading.get_ident()
    bus = EventBus(bus_config, rng=seeded_rng(), timers=SYSTEM_TIMERS)
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with subscribed(bus) as events, asyncio.TaskGroup() as group:
        watcher = group.create_task(watch_to_user(root, bus))
        try:
            assert await next_event(events) == WorkspacePanelsResync()
            while loop.time() - started < 3:
                await asyncio.sleep(0.02)
                now = loop.time()
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
        "scans_off_the_loop_thread": sum(ident != loop_thread for ident in scanned_on),
        "scans_on_the_loop_thread": sum(ident == loop_thread for ident in scanned_on),
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
    assert sample["scans_off_the_loop_thread"] > 0, sample
    assert sample["scans_on_the_loop_thread"] == 0, sample
