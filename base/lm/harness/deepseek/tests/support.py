from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from base.lm.harness import Handoff
from base.lm.harness.deepseek import BridgeConfig, DeepSeekSession
from base.lm.harness.deepseek.tests.fake_acp import FakeConnection, ScriptedProbe
from base.lm.harness.registry import FileSessionRegistry
from base.lm.pricing import PriceBook

AGENT_PID = 4242


class Harness:
    def __init__(
        self, session: DeepSeekSession, connection: FakeConnection, probe: ScriptedProbe
    ) -> None:
        self.session = session
        self.connection = connection
        self.probe = probe
        self.events: list[Any] = []
        self._drain: asyncio.Task[None] = asyncio.create_task(self._collect())

    async def _collect(self) -> None:
        async for event in self.session.events():
            self.events.append(event)

    def of(self, kind: type) -> list[Any]:
        return [event for event in self.events if isinstance(event, kind)]

    def requests(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.connection.requests if name == method]

    def notifications(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.connection.sent if name == method]

    async def settle(self) -> None:
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def wait_for(self, predicate: Callable[[Any], bool], *, timeout: float = 2.0) -> Any:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for event in self.events:
                if predicate(event):
                    return event
            await asyncio.sleep(0.002)
        raise AssertionError(f"no matching event within {timeout}s; saw {self.events!r}")

    async def prompt_in_flight(self, *, timeout: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.requests("session/prompt"):
                return
            await asyncio.sleep(0.002)
        raise AssertionError("the turn never reached session/prompt")

    async def stop(self) -> None:
        await self.session.close(reason="test done")
        self._drain.cancel()
        await asyncio.gather(self._drain, return_exceptions=True)


def config(tmp_path: Path, **overrides: Any) -> BridgeConfig:
    values: dict[str, Any] = {"cwd": tmp_path, "dsh_home": tmp_path / ".dsh"}
    values.update(overrides)
    return BridgeConfig(**values)


async def start(
    tmp_path: Path,
    *,
    scripts: dict[str, list[Any]] | None = None,
    probe: ScriptedProbe | None = None,
    resume: str | None = None,
    handoff: Handoff | None = None,
    agent_id: int | None = None,
    registry: FileSessionRegistry | None = None,
    session_id: str = "session-1",
    observe_pid: bool = True,
    prices: PriceBook | None = None,
    **config_overrides: Any,
) -> Harness:
    connection = FakeConnection(
        scripts=scripts, pid=AGENT_PID if observe_pid else None, session_id=session_id
    )
    scripted_probe = probe if probe is not None else ScriptedProbe()
    session = DeepSeekSession(
        config(tmp_path, **config_overrides),
        environ={},
        agent_id=agent_id,
        registry=registry,
        probe=scripted_probe,
        connect=lambda: connection,
        kill=lambda pid, _signal_number: scripted_probe.kill(pid),
        prices=prices,
    )
    await session.start(resume=resume, handoff=handoff)
    return Harness(session, connection, scripted_probe)
