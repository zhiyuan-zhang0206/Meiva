# meiva:note
# A wiring hook whose units spawn children that are already reaped. Installed on
# the supervisor's own spawn seam, so the daemon's other children and the rest of
# the process keep the stock launcher.

import asyncio
from typing import Any

spawn = asyncio.create_subprocess_exec


async def reaped_first(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
    proc = await spawn(*args, **kwargs)
    await proc.wait()
    return proc


def build(context: Any) -> list[object]:
    context.supervisor.spawn = reaped_first
    return []
