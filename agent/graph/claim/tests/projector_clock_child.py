from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from agent.graph.claim.harness_turn import HarnessTurnProjector
from base.agents.observation.turn_progress import TurnProgress
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.config import ConfigBoot
from base.events.live.tests.live_capture import idle_publisher
from base.lm.harness import ReasoningText, ToolResult, ToolUse


async def main() -> None:
    owner = ConfigBoot()
    owner.boot()
    clock = Clock(clock_config_from_boot(owner), timers=SYSTEM_TIMERS)
    projector = HarnessTurnProjector(
        event_publisher=idle_publisher(),
        agent_id=7,
        base_idx=0,
        turn_progress=TurnProgress(),
        clock=clock,
    )
    os.environ["MEIVA_MESSAGE_TIMESTAMP_WEEKDAY"] = "not-a-bool"
    projector.on_event(ReasoningText(turn_id="turn", text="planning"))
    projector.on_event(
        ToolUse(turn_id="turn", tool_use_id="call", name="Bash", tool_input={"command": "true"})
    )
    projector.on_event(ToolResult(turn_id="turn", tool_use_id="call", content="ok", is_error=False))
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
