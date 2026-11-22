from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from base.clock.tests.fakes import system_clock
from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.session import AgySession
from base.lm.harness.base_session import SessionBinding
from base.lm.harness.events import BridgeEvent, UnsolicitedSink

FAKE_CLI = Path(__file__).with_name("fake_agy.py")


class Harness:
    def __init__(self, session: AgySession, cwd: Path) -> None:
        self.session = session
        self.cwd = cwd
        self.home = cwd / "home"
        self.events: list[BridgeEvent] = []
        self._drain = asyncio.create_task(self._collect())

    async def _collect(self) -> None:
        async for event in self.session.events():
            self.events.append(event)

    async def wait_for(self, predicate: Callable[[Any], bool]) -> Any:
        async with asyncio.timeout(5):
            while True:
                for event in self.events:
                    if predicate(event):
                        return event
                await asyncio.sleep(0.002)

    def frames(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in (self.cwd / "frames.jsonl").read_text().splitlines()]

    def prompts(self) -> list[dict[str, Any]]:
        return [frame for frame in self.frames() if frame.get("method") == "session/prompt"]

    def release(self) -> None:
        (self.cwd / "release").touch()

    async def stop(self) -> None:
        await self.session.close(reason="test done")
        await asyncio.wait_for(self._drain, timeout=3)


async def start(
    tmp_path: Path,
    *,
    resume: str | None = None,
    unsolicited: UnsolicitedSink | None = None,
) -> Harness:
    env = {"HOME": str(tmp_path / "home")}
    session = AgySession(
        AntigravityConfig(
            cwd=tmp_path,
            model="gemini-4-hydrogen-low",
            executable=(sys.executable, str(FAKE_CLI)),
            cancel_settle_seconds=1,
        ),
        environ=env,
        binding=SessionBinding(unsolicited=unsolicited),
        clock=system_clock(),
        home=tmp_path / "home",
        timing_refresh_interval=0,
    )
    await session.start(resume=resume)
    return Harness(session, tmp_path)
