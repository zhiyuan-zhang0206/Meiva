# meiva:note
# Child of the real-SIGTERM test: `serve_until_exit` over an in-memory scheduler and one
# turn. The process installs the ordinary KeyboardInterrupt route first, as the daemon's
# `main()` does, so a SIGTERM that escaped the graceful exit would end this child with a
# traceback instead of the result line. The one turn settles only after the second SIGTERM
# has been answered as a repeat, so the parent observes both.

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from loguru import logger

from base.agents.observation.turn_progress import TurnProgress
from base.daemon.shutdown import install_graceful_shutdown
from services.agent_runner.agent_host.lifecycle.graceful_exit import OwedRestart, serve_until_exit

_AGENT = 7


def _say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class _Turns:
    def __init__(self, tasks: dict[int, asyncio.Task[None]]) -> None:
        self._tasks = tasks

    def task_for(self, agent_id: int) -> asyncio.Task[None] | None:
        return self._tasks.get(agent_id)

    def stop_admission(self) -> frozenset[int]:
        return frozenset(self._tasks)


async def _run() -> dict[str, Any]:
    repeated = asyncio.Event()
    repeats = 0

    def count_repeats(message: Any) -> None:
        nonlocal repeats
        if message.record["extra"].get("event") == "host_exit_repeated":
            repeats += 1
            repeated.set()

    logger.add(count_repeats, level="INFO")

    async def turn() -> None:
        await repeated.wait()

    restarts_for: list[int] = []

    async def write_restart(agent_id: int) -> OwedRestart:
        restarts_for.append(agent_id)
        _say("draining")
        return OwedRestart(1, written=True)

    async def dispatch() -> None:
        _say("ready")
        await asyncio.Event().wait()

    task = asyncio.create_task(turn())
    await serve_until_exit(
        dispatch(),
        turns=_Turns({_AGENT: task}),
        progress=TurnProgress(),
        write_restart=write_restart,
    )
    return {"restarts_for": restarts_for, "turn_settled": task.done(), "repeats": repeats}


if __name__ == "__main__":
    install_graceful_shutdown("graceful-exit-test")
    _say(json.dumps(asyncio.run(_run())))
