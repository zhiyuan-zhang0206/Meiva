from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from base.lm.harness.codex import CodexConfig, CodexSession
from base.lm.harness.codex.tests.fake_app_server import CODEX_PID, FakeAppServer
from base.lm.harness.codex.transport import as_object, as_text
from base.lm.harness.handoff import Handoff
from base.lm.harness.registry import FileSessionRegistry


# meiva:note
# The text of the first input item of a `turn/start` request, so a test can assert
# what the bridge actually sent without re-declaring the request shape.
def turn_text(params: object) -> str:
    data = as_object(params)
    inputs = data.get("input")
    if not isinstance(inputs, list):
        return ""
    entries = cast("list[object]", inputs)
    return as_text(as_object(entries[0]).get("text")) or "" if entries else ""


class ScriptedProbe:
    def __init__(self, children: dict[int, str] | None = None) -> None:
        self.children = dict(children or {})
        self.alive: set[int] = set(self.children)

    def descendants(self, pid: int) -> dict[int, str]:
        return dict(self.children)

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive

    def spawn(self, pid: int, command: str) -> None:
        self.children[pid] = command
        self.alive.add(pid)

    def reap(self, *pids: int) -> None:
        for pid in pids:
            self.alive.discard(pid)


class Harness:
    def __init__(self, session: CodexSession, server: FakeAppServer, probe: ScriptedProbe) -> None:
        self.session = session
        self.server = server
        self.probe = probe
        self.events: list[Any] = []
        self._drain: asyncio.Task[None] = asyncio.create_task(self._collect())

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

    async def turn_started(self, count: int = 1, *, timeout: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if len(self.server.turn_ids) >= count:
                return
            await asyncio.sleep(0.002)
        raise AssertionError(f"only {len(self.server.turn_ids)} turns started")

    async def stop(self) -> None:
        await self.session.close(reason="test done")
        self._drain.cancel()
        await asyncio.gather(self._drain, return_exceptions=True)


def config(tmp_path: Path, **overrides: Any) -> CodexConfig:
    values: dict[str, Any] = {"cwd": tmp_path}
    values.update(overrides)
    return CodexConfig(**values)


def handoff(agent_id: int = 7, *, instruction: str = "Reply with one word.") -> Handoff:
    return Handoff(
        agent_id=agent_id,
        instruction=instruction,
        machine="ubuntu",
        workspace=Path("workspace"),
    )


async def start(
    tmp_path: Path,
    *,
    server: FakeAppServer | None = None,
    probe: ScriptedProbe | None = None,
    resume: str | None = None,
    handoff_doc: Handoff | None = None,
    agent_id: int | None = None,
    registry: FileSessionRegistry | None = None,
    observe_pid: bool = True,
    **config_overrides: Any,
) -> Harness:
    fake = server if server is not None else FakeAppServer(pid=CODEX_PID if observe_pid else None)
    scripted_probe = probe if probe is not None else ScriptedProbe()
    if observe_pid and fake.pid is not None:
        scripted_probe.alive.add(fake.pid)
    session = CodexSession(
        config(tmp_path, **config_overrides),
        environ={},
        agent_id=agent_id,
        registry=registry,
        transport=fake,
        probe=scripted_probe,
    )
    await session.start(resume=resume, handoff=handoff_doc)
    return Harness(session, fake, scripted_probe)
