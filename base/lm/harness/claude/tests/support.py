from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from base.lm.harness import Handoff
from base.lm.harness.claude import BridgeConfig, ClaudeSession
from base.lm.harness.claude.tests.fake_sdk import FakeSDK, ScriptedProbe
from base.lm.harness.registry import FileSessionRegistry

CLI_PID = 4242


class Harness:
    def __init__(self, session: ClaudeSession, sdk: FakeSDK, probe: ScriptedProbe) -> None:
        self.session = session
        self.sdk = sdk
        self.probe = probe
        self.events: list[Any] = []
        self._drain: asyncio.Task[None] = asyncio.create_task(self._collect())

    @property
    def client(self) -> Any:
        return self.sdk.clients[0]

    async def _collect(self) -> None:
        async for event in self.session.events():
            self.events.append(event)

    def of(self, kind: type) -> list[Any]:
        return [event for event in self.events if isinstance(event, kind)]

    async def wait_for(self, predicate: Callable[[Any], bool], *, timeout: float = 2.0) -> Any:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for event in self.events:
                if predicate(event):
                    return event
            await asyncio.sleep(0.002)
        raise AssertionError(f"no matching event within {timeout}s; saw {self.events!r}")

    async def detach_consumer(self) -> None:
        self._drain.cancel()
        await asyncio.gather(self._drain, return_exceptions=True)

    async def stop(self) -> None:
        await self.session.close(reason="test done")
        self._drain.cancel()
        await asyncio.gather(self._drain, return_exceptions=True)


def config(tmp_path: Path, **overrides: Any) -> BridgeConfig:
    values: dict[str, Any] = {"cwd": tmp_path}
    values.update(overrides)
    return BridgeConfig(**values)


async def start(
    tmp_path: Path,
    *,
    scripts: list[list[Any]] | None = None,
    probe: ScriptedProbe | None = None,
    resume: str | None = None,
    handoff: Handoff | None = None,
    agent_id: int | None = None,
    registry: FileSessionRegistry | None = None,
    observe_pid: bool = True,
    environ: dict[str, str] | None = None,
    session_id: str = "session-1",
    **config_overrides: Any,
) -> Harness:
    sdk = FakeSDK(scripts, cli_pid=CLI_PID)
    sdk.new_session_id = session_id
    if not observe_pid:
        create_client = sdk.ClaudeSDKClient

        def without_transport(options: Any) -> Any:
            client = create_client(options)
            client._transport = None
            return client

        sdk.ClaudeSDKClient = without_transport
    scripted_probe = probe if probe is not None else ScriptedProbe()
    sdk.probe = scripted_probe
    if observe_pid:
        scripted_probe.alive.add(CLI_PID)
    session = ClaudeSession(
        config(tmp_path, **config_overrides),
        environ=environ if environ is not None else {},
        agent_id=agent_id,
        registry=registry,
        sdk=sdk,
        probe=scripted_probe,
    )
    await session.start(resume=resume, handoff=handoff)
    return Harness(session, sdk, scripted_probe)
