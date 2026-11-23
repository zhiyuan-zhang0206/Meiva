from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from base.clock.tests.fakes import system_clock
from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.session import AgySession
from base.lm.harness.antigravity.transport import AgyTransport
from base.lm.harness.base_session import SessionBinding
from base.lm.harness.events import BridgeEvent, ToolResult, UnsolicitedSink
from base.lm.harness.registry import SessionRegistry
from base.lm.harness.session import SessionClock

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

    async def wait_for_type[T](self, event_type: type[T]) -> T:
        event: object = await self.wait_for(lambda value: isinstance(value, event_type))
        assert isinstance(event, event_type)
        return event

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

    async def wait_for_marker(self, name: str) -> None:
        async with asyncio.timeout(3):
            while not (self.cwd / name).exists():
                await asyncio.sleep(0.005)

    async def stop(self) -> None:
        await self.session.close(reason="test done")
        await asyncio.wait_for(self._drain, timeout=3)


async def release_timer(harness: Harness) -> ToolResult:
    harness.release("release-tool")
    return await harness.wait_for(lambda e: isinstance(e, ToolResult) and e.tool_use_id == "timer")


async def start(
    tmp_path: Path,
    *,
    resume: str | None = None,
    unsolicited: UnsolicitedSink | None = None,
    extra_args: tuple[str, ...] = (),
    cancel_seconds: float = 1.0,
    write_seconds: float = 1.0,
    reply_seconds: float = 30.0,
    clock: SessionClock | None = None,
    registry: SessionRegistry | None = None,
) -> Harness:
    env = {"HOME": str(tmp_path / "home")}
    clock = clock if clock is not None else system_clock()
    config = AntigravityConfig(
        cwd=tmp_path,
        model="gemini-4-hydrogen-low",
        executable=(sys.executable, str(FAKE_CLI)),
        extra_args=extra_args,
        cancel_settle_seconds=cancel_seconds,
        prompt_write_seconds=write_seconds,
        background_reply_seconds=reply_seconds,
    )
    session = AgySession(
        config,
        environ=env,
        transport_factory=lambda: AgyTransport(
            config, environ=env, owner_uid=os.getuid(), generation_id=uuid4().hex, clock=clock
        ),
        binding=SessionBinding(
            agent_id=1 if registry is not None else None, registry=registry, unsolicited=unsolicited
        ),
        clock=clock,
        home=tmp_path / "home",
        timing_refresh_interval=0,
    )
    await session.start(resume=resume)
    return Harness(session, tmp_path)


def seed_replay_history(tmp_path: Path) -> None:
    transcript = (
        tmp_path
        / "home/.gemini/antigravity-cli/brain/conv-acp"
        / ".system_generated/logs/transcript_full.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    rows = (
        {"type": "PLANNER_RESPONSE", "content": "REPLAYED_OLD_TEXT"},
        {
            "type": "SYSTEM_MESSAGE",
            "content": "[Message] timestamp=2026-11-16T19:38:44Z sender=conv-acp/task-1 priority=MESSAGE_PRIORITY_HIGH content=former timer",
        },
        {"type": "PLANNER_RESPONSE", "content": "MEIVA_DISPLAY_TO_USER\nOld native reply"},
    )
    _ = transcript.write_text(
        "".join(
            json.dumps({"status": "DONE", "created_at": "2026-11-16T19:38:44Z", **row}) + "\n"
            for row in rows
        )
    )
