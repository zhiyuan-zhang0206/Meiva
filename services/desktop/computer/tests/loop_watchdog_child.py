# meiva:note
# A child process that runs the real loop watchdog. `blocked` stops the event loop dead, which the
# watchdog must answer by dumping the stacks and exiting 1; `beating` keeps the loop alive for
# longer than the window, which must end with a clean exit 0.

from __future__ import annotations

import asyncio
import sys
import time

from services.desktop.computer import mcp_daemon


async def main(mode: str) -> None:
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
