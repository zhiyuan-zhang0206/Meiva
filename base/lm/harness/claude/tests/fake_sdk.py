from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = ""
    is_error: bool | None = False


@dataclass
class AssistantMessage:
    content: list[Any]
    model: str = "claude-sonnet-5-5"
    parent_tool_use_id: str | None = None


@dataclass
class UserMessage:
    content: Any


@dataclass
class SystemMessage:
    subtype: str
    data: dict[str, Any]


@dataclass
class StreamEvent:
    event: dict[str, Any]
    uuid: str = "stream-1"
    session_id: str = "session-1"
    parent_tool_use_id: str | None = None


@dataclass
class RateLimitInfo:
    status: str
    resets_at: int | None = None
    rate_limit_type: str | None = None
    utilization: float | None = None


@dataclass
class RateLimitEvent:
    rate_limit_info: RateLimitInfo
    uuid: str = "rate-1"
    session_id: str = "session-1"


@dataclass
class ResultMessage:
    subtype: str = "success"
    duration_ms: int = 1200
    duration_api_ms: int = 900
    is_error: bool = False
    num_turns: int = 1
    session_id: str = "session-1"
    stop_reason: str | None = "end_turn"
    total_cost_usd: float | None = 0.0004
    usage: dict[str, Any] | None = field(
        default_factory=lambda: {
            "input_tokens": 4,
            "output_tokens": 138,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 2738,
        }
    )
    result: str | None = None
    model_usage: dict[str, Any] | None = None
    permission_denials: list[Any] | None = None


@dataclass
class ClaudeAgentOptions:
    model: str | None = None
    cwd: str | None = None
    tools: list[str] | None = None
    permission_mode: str | None = None
    max_turns: int | None = None
    setting_sources: list[str] | None = None
    strict_mcp_config: bool = False
    include_partial_messages: bool = False
    env: dict[str, str] = field(default_factory=dict[str, str])
    resume: str | None = None


@dataclass(frozen=True)
class Emit:
    message: Any


@dataclass(frozen=True)
class Say:
    text: str


@dataclass(frozen=True)
class CallTool:
    name: str
    tool_input: dict[str, Any]
    tool_use_id: str = "toolu-1"
    content: str = "ok"
    is_error: bool = False


@dataclass(frozen=True)
class Block:
    pass


@dataclass(frozen=True)
class Delay:
    seconds: float


_END = object()


class FakeClaudeSDKClient:
    def __init__(self, options: ClaudeAgentOptions, *, sdk: FakeSDK) -> None:
        self.options = options
        self._sdk = sdk
        self.queries: list[str] = []
        self.timeline: list[tuple[str, Any]] = []
        self.interrupts = 0
        self.connected = False
        self.disconnected = False
        self.on_interrupt: Any = None
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._turn: asyncio.Task[None] | None = None
        self._blocker = asyncio.Event()
        self._transport: SimpleNamespace | None = SimpleNamespace(
            _process=SimpleNamespace(pid=sdk.cli_pid)
        )

    async def connect(self) -> None:
        self.connected = True
        self._emit(
            SystemMessage(
                subtype="init",
                data={
                    "session_id": self.options.resume or self._sdk.new_session_id,
                    "model": self.options.model,
                    "cwd": self.options.cwd,
                    "apiKeySource": "none",
                },
            )
        )

    async def disconnect(self) -> None:
        self.disconnected = True
        await self._cancel_turn()
        self._queue.put_nowait(_END)

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        self.timeline.append(("query", prompt))
        self._blocker = asyncio.Event()
        script = self._sdk.scripts.pop(0) if self._sdk.scripts else []
        self._turn = asyncio.create_task(self._run(script))

    async def _run(self, script: list[Any]) -> None:
        try:
            for step in script:
                if isinstance(step, Emit):
                    self._emit(step.message)
                elif isinstance(step, Say):
                    self._emit(_delta(step.text))
                    self._emit(AssistantMessage(content=[TextBlock(text=step.text)]))
                elif isinstance(step, Delay):
                    await asyncio.sleep(step.seconds)
                elif isinstance(step, CallTool):
                    self._emit(
                        AssistantMessage(
                            content=[
                                ToolUseBlock(
                                    id=step.tool_use_id, name=step.name, input=step.tool_input
                                )
                            ]
                        )
                    )
                    self._emit(
                        UserMessage(
                            content=[
                                ToolResultBlock(
                                    tool_use_id=step.tool_use_id,
                                    content=step.content,
                                    is_error=step.is_error,
                                )
                            ]
                        )
                    )
                elif isinstance(step, Block):
                    await self._blocker.wait()
                else:
                    raise TypeError(f"unknown script step: {step!r}")
        except asyncio.CancelledError:
            return
        self._finish(result=ResultMessage())

    def _finish(self, *, result: ResultMessage) -> None:
        self.timeline.append(("result", result.subtype))
        self._emit(result)

    async def interrupt(self) -> None:
        self.interrupts += 1
        await self._cancel_turn()
        if self.on_interrupt is not None:
            self.on_interrupt()
        self._finish(result=ResultMessage(subtype="error_during_execution", is_error=True))

    async def _cancel_turn(self) -> None:
        turn, self._turn = self._turn, None
        if turn is None or turn.done():
            return
        turn.cancel()
        await asyncio.wait({turn})

    async def receive_messages(self):
        while True:
            message = await self._queue.get()
            if message is _END:
                return
            yield message

    async def __aenter__(self) -> FakeClaudeSDKClient:
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> bool:
        await self.disconnect()
        return False

    def _emit(self, message: Any) -> None:
        self._queue.put_nowait(message)


class FakeSDK:
    def __init__(self, scripts: list[list[Any]] | None = None, *, cli_pid: int = 4242) -> None:
        self.scripts: list[list[Any]] = list(scripts or [])
        self.cli_pid = cli_pid
        self.new_session_id = "session-1"
        self.clients: list[FakeClaudeSDKClient] = []
        self.ClaudeAgentOptions = ClaudeAgentOptions

    def ClaudeSDKClient(self, options: ClaudeAgentOptions) -> FakeClaudeSDKClient:
        client = FakeClaudeSDKClient(options, sdk=self)
        self.clients.append(client)
        return client


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


def _delta(text: str) -> StreamEvent:
    return StreamEvent(
        event={
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }
    )
