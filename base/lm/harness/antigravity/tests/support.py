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

    def release(self, name: str = "release") -> None:
        (self.cwd / name).touch()

    async def wait_for_method(self, method: str) -> None:
        async with asyncio.timeout(5):
            while not any(frame.get("method") == method for frame in self.frames()):
                await asyncio.sleep(0.005)

    async def stop(self) -> None:
        await self.session.close(reason="test done")
        await asyncio.wait_for(self._drain, timeout=3)


async def start(
    tmp_path: Path,
    *,
    resume: str | None = None,
    unsolicited: UnsolicitedSink | None = None,
    extra_args: tuple[str, ...] = (),
    cancel_seconds: float = 1.0,
    write_seconds: float = 1.0,
    reply_seconds: float = 30.0,
) -> Harness:
    env = {"HOME": str(tmp_path / "home")}
    session = AgySession(
        AntigravityConfig(
            cwd=tmp_path,
            model="gemini-4-hydrogen-low",
            executable=(sys.executable, str(FAKE_CLI)),
            extra_args=extra_args,
            cancel_settle_seconds=cancel_seconds,
            prompt_write_seconds=write_seconds,
            background_reply_seconds=reply_seconds,
        ),
        environ=env,
        binding=SessionBinding(unsolicited=unsolicited),
        clock=system_clock(),
        home=tmp_path / "home",
        timing_refresh_interval=0,
    )
    await session.start(resume=resume)
    return Harness(session, tmp_path)
