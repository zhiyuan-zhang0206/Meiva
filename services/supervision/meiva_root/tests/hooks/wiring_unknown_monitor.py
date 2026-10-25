import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

from services.supervision.meiva_root.supervisor import SupervisorConfig


class Monitor:
    def __init__(self, tasks: asyncio.TaskGroup | None) -> None:
        self._tasks = tasks
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._tasks is None:
            raise RuntimeError("monitor requires the root participant task group")
        self._task = self._tasks.create_task(self._loop())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(0.01)
            if Path(os.environ["MEIVA_ROOT_WIRING_FAULT"]).exists():
                raise ValueError("unknown-monitor-primary")


def build(context: Any) -> Monitor:
    context.supervisor._config = SupervisorConfig(stop_timeout_s=0.15)
    return Monitor(tasks=context.participant_tasks)
