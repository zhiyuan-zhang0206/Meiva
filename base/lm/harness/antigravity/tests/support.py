from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.session import AgySession
from base.lm.harness.antigravity.transport import AgyTransport, Reaper
from base.lm.harness.process import ChildProbe

FAKE_CLI = Path(__file__).with_name("fake_agy.py")
CLI_PID = 4242


# meiva:note
# The adapter's view of a process tree, without spawning one.
class ScriptedProbe(ChildProbe):
    def __init__(self) -> None:
        self.children: dict[int, str] = {}
        self.alive: set[int] = set()

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
    def __init__(self, session: AgySession, received: Path, argv: Path, child: Path) -> None:
        self.session = session
        self.received = received
        self.argv = argv
        self.child = child
        self.events: list[Any] = []
        self._drain = asyncio.create_task(self._collect())

    async def _collect(self) -> None:
        async for event in self.session.events():
            self.events.append(event)

    def of(self, kind: type) -> list[Any]:
        return [event for event in self.events if isinstance(event, kind)]

    async def wait_for(self, predicate: Callable[[Any], bool], *, timeout: float = 5.0) -> Any:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for event in self.events:
                if predicate(event):
                    return event
            await asyncio.sleep(0.002)
        raise AssertionError(f"no matching event within {timeout}s; saw {self.events!r}")

    def messages(self) -> list[str]:
        if not self.received.exists():
            return []
        return [
            json.loads(line)
            for line in self.received.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def child_pid(self) -> int | None:
        if not self.child.exists():
            return None
        lines = [line for line in self.child.read_text(encoding="utf-8").splitlines() if line]
        return int(json.loads(lines[-1])) if lines else None

    async def wait_until(self, predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.005)
        raise AssertionError(f"condition not met within {timeout}s")

    def argv_seen(self) -> list[str]:
        if not self.argv.exists():
            return []
        return json.loads(self.argv.read_text(encoding="utf-8").splitlines()[0])

    async def stop(self) -> None:
        await self.session.close(reason="test done")
        try:
            await asyncio.wait_for(self._drain, timeout=2.0)
        except TimeoutError:
            self._drain.cancel()
            await asyncio.gather(self._drain, return_exceptions=True)


def config(tmp_path: Path, **overrides: Any) -> AntigravityConfig:
    values: dict[str, Any] = {
        "cwd": tmp_path,
        "executable": (sys.executable, str(FAKE_CLI)),
        "turn_timeout_seconds": 30.0,
        "cancel_settle_seconds": 2.0,
    }
    values.update(overrides)
    return AntigravityConfig(**values)


async def start(
    tmp_path: Path,
    *,
    turns: Sequence[dict[str, Any]] | None = None,
    probe: ScriptedProbe | None = None,
    resume: str | None = None,
    handoff: Any | None = None,
    agent_id: int | None = None,
    registry: Any | None = None,
    conversation_id: str = "conv-fake",
    reap: Reaper | None = None,
    **config_overrides: Any,
) -> Harness:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"conversation_id": conversation_id, "turns": list(turns or [])}))
    received = tmp_path / "received.jsonl"
    argv = tmp_path / "argv.jsonl"
    child = tmp_path / "child.jsonl"
    settings = config(tmp_path, **config_overrides)
    environ = {
        "FAKE_AGY_PLAN": str(plan),
        "FAKE_AGY_RECEIVED": str(received),
        "FAKE_AGY_ARGV": str(argv),
        "FAKE_AGY_CHILD": str(child),
        "FAKE_AGY_CURSOR": str(tmp_path / "cursor.txt"),
        "PATH": "/usr/bin:/bin",
    }

    def factory() -> AgyTransport:
        return AgyTransport(settings, environ=environ, probe=probe, reap=reap)

    session = AgySession(
        settings,
        environ=environ,
        agent_id=agent_id,
        registry=registry,
        probe=probe,
        transport_factory=factory,
    )
    await session.start(resume=resume, handoff=handoff)
    return Harness(session, received, argv, child)


# meiva:note
# One assistant text step, as `agy` frames it.
def say(text: str, *, step_index: int = 1) -> dict[str, Any]:
    return {
        "steps": [
            {
                "step_index": step_index,
                "state": "ACTIVE",
                "step_type": "agent_response",
                "text_delta": text,
            },
            {
                "step_index": step_index,
                "state": "DONE",
                "step_type": "agent_response",
                "text_delta": "",
            },
        ],
        "result": {"response": text},
    }
