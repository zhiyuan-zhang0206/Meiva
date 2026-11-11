from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from base.clock.tests.fakes import system_clock
from base.lm.harness import Handoff
from base.lm.harness.base_session import SessionBinding
from base.lm.harness.claude.config import BridgeConfig
from base.lm.harness.claude.delivery import key_uuid
from base.lm.harness.claude.session import ClaudeSession
from base.lm.harness.claude.tests import fake_sdk as fake
from base.lm.harness.claude.tests.fake_sdk import FakeSDK, ScriptedProbe
from base.lm.harness.events import UnsolicitedSink
from base.lm.harness.registry import FileSessionRegistry

CLI_PID = 4242


def _blocks(raw: list[dict[str, Any]]) -> list[Any]:
    blocks: list[Any] = []
    for item in raw:
        if "text" in item:
            blocks.append(fake.TextBlock(text=item["text"]))
        elif "tool" in item:
            blocks.append(fake.ToolUseBlock(**item["tool"]))
        else:
            blocks.append(fake.ToolResultBlock(**item["tool_result"]))
    return blocks


# meiva:note
# Replay recorded CLI messages into a fake client, in order. A fixture is a JSON file of named frame
# groups; `$name` in a `uuid` stands for the uuid the session derived from the key `keys[name]`, so
# the recording works with any key. Only the frame fields the adapter reads are kept.
def replay(client: Any, fixture: str, group: str, **keys: str) -> None:
    path = Path(__file__).parent / "fixtures" / f"{fixture}.json"
    frames: list[dict[str, Any]] = json.loads(path.read_text())["frames"][group]
    for frame in frames:
        kind = frame["cls"]
        uuid = frame.get("uuid")
        if isinstance(uuid, str) and uuid.startswith("$"):
            uuid = key_uuid(keys[uuid[1:]])
        if kind == "UserMessage":
            content: Any = frame["content"]
            if isinstance(content, list):
                content = _blocks(cast("list[dict[str, Any]]", content))
            client.push(fake.UserMessage(content=content, uuid=uuid, origin=frame.get("origin")))
        elif kind == "AssistantMessage":
            client.push(fake.AssistantMessage(content=_blocks(frame["blocks"])))
        elif kind == "SystemMessage":
            client.push(fake.SystemMessage(subtype=frame["subtype"], data=frame["data"]))
        else:
            fields = {k: v for k, v in frame.items() if k != "cls"}
            client.push(_FRAME_CLASSES[kind](**_task_defaults(kind, fields)))


_FRAME_CLASSES: dict[str, Any] = {
    "ResultMessage": fake.ResultMessage,
    "TaskStartedMessage": fake.TaskStartedMessage,
    "TaskNotificationMessage": fake.TaskNotificationMessage,
}


def _task_defaults(kind: str, fields: dict[str, Any]) -> dict[str, Any]:
    if kind.startswith("Task"):
        return {"subtype": kind, "data": {}, **fields}
    return fields


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

    async def turn_started(self, *, timeout: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.session.active_turn_id is not None:
                return
            await asyncio.sleep(0.002)
        raise AssertionError("turn did not start within timeout")

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
    unsolicited: UnsolicitedSink | None = None,
    passive: bool = False,
    fork_source_agent_id: int | None = None,
    sdk: FakeSDK | None = None,
    **config_overrides: Any,
) -> Harness:
    sdk = sdk if sdk is not None else FakeSDK(scripts, cli_pid=CLI_PID)
    if scripts is not None:
        sdk.scripts = list(scripts)
    sdk.new_session_id = session_id
    sdk.passive = passive
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
        binding=SessionBinding(agent_id, registry, unsolicited, fork_source_agent_id),
        sdk=sdk,
        probe=scripted_probe,
        clock=system_clock(),
    )
    await session.start(resume=resume, handoff=handoff)
    return Harness(session, sdk, scripted_probe)
