from __future__ import annotations

import asyncio
from typing import Any, cast

from base.lm.harness.acp import AcpRpcError, AcpTimeoutError
from base.lm.harness.acp.transport import AcpNotification


class FakeConnection:
    def __init__(
        self,
        *,
        scripts: dict[str, list[Any]] | None = None,
        pid: int | None = 4242,
        session_id: str = "session-1",
        fail_resume: bool = False,
    ) -> None:
        self.scripts = scripts or {}
        self.pid = pid
        # meiva:note
        # The exit status the session records on close (task #5616); a fake
        # connection has no child process, so None unless a test sets one.
        self.returncode: int | None = None
        self.session_id = session_id
        self.fail_resume = fail_resume
        self.closed = False
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def start(self) -> None:
        return None

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        payload = dict(params or {})
        self.requests.append((method, payload))
        if method == "initialize":
            return {"protocolVersion": 1, "agentInfo": {"name": "kimi-code", "version": "1.0.0"}}
        if method == "session/new":
            return {"sessionId": self.session_id, "configOptions": []}
        if method == "session/resume":
            if self.fail_resume:
                raise AcpRpcError("session/resume", -32000, "session not found")
            return {"configOptions": []}
        if method == "session/close":
            return {}
        if method == "session/set_config_option":
            return {}
        if method == "session/prompt":
            return await self._prompt(payload)
        raise AssertionError(f"unexpected request {method}")

    async def _prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        blocks = cast("list[object]", params.get("prompt", []))
        text = "".join(
            str(cast("dict[str, Any]", block).get("text", ""))
            for block in blocks
            if isinstance(block, dict)
        )
        if "timeout" in text:
            raise AcpTimeoutError("session/prompt timed out")
        if "slow" in text:
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._pending[str(params["sessionId"])] = future
            return await future
        if "think:" in text:
            await self.emit(
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "pondering the prompt"},
                }
            )
        await self.emit(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": f"echo:{text}"},
            }
        )
        await self.emit({"sessionUpdate": "usage_update", "used": 12, "size": 1000})
        return {"stopReason": "end_turn"}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.sent.append((method, dict(params or {})))
        if method == "session/cancel":
            session_id = str((params or {}).get("sessionId"))
            pending = self._pending.pop(session_id, None)
            if pending is not None and not pending.done():
                pending.set_result({"stopReason": "cancelled"})

    async def emit(self, update: dict[str, Any], *, session_id: str | None = None) -> None:
        await self._queue.put(
            AcpNotification(
                method="session/update",
                params={"sessionId": session_id or self.session_id, "update": update},
            )
        )

    async def notifications(self) -> Any:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def close(self, *, reason: str = "closed") -> None:
        self.closed = True
        await self._queue.put(None)


class ScriptedProbe:
    def __init__(self) -> None:
        self.children: dict[int, str] = {}
        self.alive: set[int] = set()
        self.stubborn: set[int] = set()
        self.killed: list[int] = []

    def descendants(self, pid: int) -> dict[int, str]:
        return dict(self.children)

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive

    def spawn(self, pid: int, command: str, *, stubborn: bool = False) -> None:
        self.children[pid] = command
        self.alive.add(pid)
        if stubborn:
            self.stubborn.add(pid)

    def reap(self, pid: int) -> None:
        self.alive.discard(pid)

    def kill(self, pid: int) -> None:
        self.killed.append(pid)
        if pid not in self.stubborn:
            self.alive.discard(pid)
