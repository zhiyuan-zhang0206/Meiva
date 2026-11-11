from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from base.clock.tests.fakes import system_clock
from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.session import AgySession
from base.lm.harness.antigravity.transport import AgyTransport, Reaper
from base.lm.harness.base_session import SessionBinding
from base.lm.harness.events import UnsolicitedSink
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
    def __init__(
        self, session: AgySession, received: Path, argv: Path, child: Path, rpc: Path | None = None
    ) -> None:
        self.session = session
        self.received = received
        self.rpc = rpc or child.with_name("rpc.jsonl")
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

    def rpc_payloads(self) -> list[dict[str, Any]]:
        if not self.rpc.exists():
            return []
        return [json.loads(line) for line in self.rpc.read_text().splitlines() if line.strip()]

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

    async def turn_started(self) -> None:
        await self.wait_until(lambda: self.session.active_turn_id is not None)

    def argv_seen(self) -> list[str]:
        if not self.argv.exists():
            return []
        lines = [line for line in self.argv.read_text(encoding="utf-8").splitlines() if line]
        return json.loads(lines[0]) if lines else []

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
    unsolicited: UnsolicitedSink | None = None,
    conversation_id: str = "conv-fake",
    reap: Reaper | None = None,
    extra_environ: dict[str, str] | None = None,
    **config_overrides: Any,
) -> Harness:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"conversation_id": conversation_id, "turns": list(turns or [])}))
    received = tmp_path / "received.jsonl"
    argv = tmp_path / "argv.jsonl"
    child = tmp_path / "child.jsonl"
    settings = config(tmp_path, **config_overrides)
    home = tmp_path / "home"
    logs = (
        home
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / conversation_id
        / ".system_generated"
        / "logs"
    )
    logs.mkdir(parents=True)
    rpc = tmp_path / "rpc.jsonl"
    environ = {
        "FAKE_AGY_PLAN": str(plan),
        "FAKE_AGY_RECEIVED": str(received),
        "FAKE_AGY_ARGV": str(argv),
        "FAKE_AGY_CHILD": str(child),
        "FAKE_AGY_CURSOR": str(tmp_path / "cursor.txt"),
        "FAKE_AGY_RPC": str(rpc),
        "FAKE_AGY_TRANSCRIPT": str(logs / "transcript.jsonl"),
        "PATH": "/usr/bin:/bin",
        **(extra_environ or {}),
    }

    def factory() -> AgyTransport:
        return AgyTransport(settings, environ=environ, probe=probe, reap=reap, clock=system_clock())

    session = AgySession(
        settings,
        environ=environ,
        binding=SessionBinding(agent_id, registry, unsolicited),
        probe=probe,
        transport_factory=factory,
        clock=system_clock(),
        home=home,
    )
    await session.start(resume=resume, handoff=handoff)
    return Harness(session, received, argv, child, rpc)


# meiva:note
# The turn is running inside the fake CLI: it has the message and has reported its conversation id.
# `turn_started()` only says our ticket was sent, and a delivery that lands before the CLI picked the
# input up correctly waits behind the ticket instead of steering.
async def running(harness: Harness, sent: list[str]) -> None:
    await harness.wait_until(lambda: harness.messages() == sent)
    await harness.wait_until(lambda: harness.session.session_id is not None)
    await asyncio.sleep(0.1)


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
