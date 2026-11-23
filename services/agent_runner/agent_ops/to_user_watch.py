import asyncio
import os
import stat
from itertools import count
from math import ceil
from pathlib import Path
from threading import Event
from typing import Protocol

from base.agents.to_user.models import DOCUMENT_FILENAME, ONGOING_FILENAME
from base.events.live.projection import OngoingChanged, ToUserChanged, WorkspacePanelsResync
from base.host.net.resilience import ExponentialBackoff

type FileStamp = tuple[int, int, int, int, int]
type WorkspaceStamp = tuple[int, int, FileStamp | None]


class _Publisher(Protocol):
    async def publish_best_effort(self, payload: str, *, context: str) -> int | None: ...


def _file_stamp(path: Path) -> FileStamp | None:
    try:
        value = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _snapshot(root: Path, stopped: Event) -> tuple[dict[int, WorkspaceStamp], dict[int, FileStamp]]:
    workspaces: dict[int, WorkspaceStamp] = {}
    ongoing: dict[int, FileStamp] = {}
    with os.scandir(root) as entries:
        for entry in entries:
            if stopped.is_set():
                break
            if not entry.name.isdecimal():
                continue
            agent_id = int(entry.name)
            if agent_id <= 0 or entry.name != str(agent_id):
                continue
            try:
                value = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(value.st_mode):
                workspace = root / entry.name
                workspaces[agent_id] = (
                    value.st_dev,
                    value.st_ino,
                    _file_stamp(workspace / DOCUMENT_FILENAME),
                )
                if (stamp := _file_stamp(workspace / ONGOING_FILENAME)) is not None:
                    ongoing[agent_id] = stamp
    return workspaces, ongoing


def _changed[Stamp](previous: dict[int, Stamp], current: dict[int, Stamp]) -> list[int]:
    return sorted(
        agent_id
        for agent_id in previous.keys() | current.keys()
        if previous.get(agent_id) != current.get(agent_id)
    )


async def watch_to_user(root: Path, bus: _Publisher) -> None:

    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    stopped = Event()
    scan_period_s = 0.5
    try:
        previous_to_user, previous_ongoing = await asyncio.to_thread(_snapshot, root, stopped)
        pending_resync: str | None = WorkspacePanelsResync().model_dump_json()
        backoff = ExponentialBackoff()
        next_resync_scan = retry_attempt = 0
        for scan in count():
            if pending_resync is not None and scan >= next_resync_scan:
                published = await bus.publish_best_effort(
                    pending_resync, context="workspace_panels_resync"
                )
                if published is not None:
                    pending_resync = None
                else:
                    delay = backoff(retry_attempt)
                    next_resync_scan = scan + ceil(delay / scan_period_s)
                    if delay < backoff.cap:
                        retry_attempt += 1
            await asyncio.sleep(scan_period_s)
            to_user, ongoing = await asyncio.to_thread(_snapshot, root, stopped)
            for agent_id in _changed(previous_to_user, to_user):
                await bus.publish_best_effort(
                    ToUserChanged(agent_id=agent_id).model_dump_json(), context="to_user"
                )
            for agent_id in _changed(previous_ongoing, ongoing):
                await bus.publish_best_effort(
                    OngoingChanged(agent_id=agent_id).model_dump_json(), context="ongoing"
                )
            previous_to_user, previous_ongoing = to_user, ongoing
    finally:
        stopped.set()
