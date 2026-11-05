from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from agent.graph.claim.harness_activity import HarnessTurnProjector
from base.agents.history.timeline_item import TimelineItem
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.config import ConfigBoot
from base.lm.harness import ReasoningText, ToolResult, ToolUse


async def main() -> None:
    owner = ConfigBoot()
    owner.boot()
    clock = Clock(clock_config_from_boot(owner), timers=SYSTEM_TIMERS)

    async def checkpoint(item: TimelineItem, msg_count: int) -> None:
        pass

    projector = HarnessTurnProjector(checkpoint=checkpoint, base_idx=0, clock=clock)
    os.environ["MEIVA_MESSAGE_TIMESTAMP_WEEKDAY"] = "not-a-bool"
    await projector.on_event(ReasoningText(turn_id="turn", text="planning"))
    await projector.on_event(
        ToolUse(turn_id="turn", tool_use_id="call", name="Bash", tool_input={"command": "true"})
    )
    await projector.on_event(
        ToolResult(turn_id="turn", tool_use_id="call", content="ok", is_error=False)
    )
    result = {"projected": True, "next_capture_refused": False, "initial_bad_refused": False}
    try:
        clock_config_from_boot(owner)
    except ValueError:
        result["next_capture_refused"] = True
    Path(os.environ["MEIVA_HOME"], ".env").write_text(
        "MEIVA_MESSAGE_TIMESTAMP_WEEKDAY=not-a-bool\n", encoding="utf-8"
    )
    try:
        Clock(clock_config_from_boot(ConfigBoot()), timers=SYSTEM_TIMERS)
    except ValueError:
        result["initial_bad_refused"] = True
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
