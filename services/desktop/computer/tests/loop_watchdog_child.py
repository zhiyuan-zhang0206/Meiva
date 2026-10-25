# meiva:note
# A child process that runs the real loop watchdog or bounded cleanup. `blocked` stops the event
# loop dead, which the watchdog must answer by dumping the stacks and exiting 1; `beating` keeps
# the loop alive for longer than the window, which must end with a clean exit 0; `stubborn-cleanup`
# runs `bounded_cleanup` against a client task that ignores cancellation so `dump_and_exit` fires.

from __future__ import annotations

import asyncio
import sys
import time
from typing import cast

from services.desktop.computer import mcp_daemon


class _FakeServer:
    async def wait_closed(self) -> None:
        return None


async def _stubborn() -> None:
    while True:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            continue


async def main(mode: str) -> None:
    if mode == "stubborn-cleanup":
        task = asyncio.create_task(_stubborn())
        await asyncio.sleep(0)
        await mcp_daemon.bounded_cleanup(
            cast(asyncio.AbstractServer, _FakeServer()), {task}, drain_s=0.05
        )
        return
    watchdog = mcp_daemon.guard_loop_liveness(
        asyncio.get_running_loop(), 1.0, beat_s=0.1, check_s=0.05
    )
    if mode == "blocked":
        time.sleep(30)
    else:
        await asyncio.sleep(2.5)
    watchdog.stop()
    watchdog.join(2.0)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
