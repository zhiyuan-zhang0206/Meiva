from __future__ import annotations

import asyncio
from collections.abc import Callable
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
    uuid: str | None = None
    origin: dict[str, Any] | None = None
    tool_use_result: dict[str, Any] | None = None


@dataclass
class SystemMessage:
    subtype: str
    data: dict[str, Any]


# meiva:note
# The three task messages the CLI emits about a background command, with the SDK's
# own field names. `TaskNotificationMessage` and the other two are `SystemMessage`
# subclasses in the real SDK too, so the adapter's dispatch — by class name, before
# the `SystemMessage` entry — is exercised exactly as it is against the CLI.
# meiva:link base/lm/harness/claude/docs/claude.md
@dataclass
class TaskStartedMessage(SystemMessage):
    task_id: str = "task-1"
    description: str = ""
    task_type: str = ""


@dataclass
class TaskNotificationMessage(SystemMessage):
    task_id: str = "task-1"
    status: str = "completed"
    output_file: str = ""
    summary: str = ""


@dataclass
class TaskUpdatedMessage(SystemMessage):
    task_id: str = "task-1"
    patch: dict[str, Any] = field(default_factory=dict[str, Any])


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
    origin: dict[str, Any] | None = None
    errors: list[str] | None = None
    api_error_status: int | None = None
    terminal_reason: str | None = None


@dataclass
class ClaudeAgentOptions:
    system_prompt: str | dict[str, object] | None = None
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
    extra_args: dict[str, str | None] | None = None
    fork_session: bool = False
    session_id: str | None = None


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
    structured: dict[str, Any] | None = None


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
        self.stopped: list[str] = []
        self.connected = False
        self.disconnected = False
        self.on_interrupt: Any = None
        self.interrupt_ack_gate: asyncio.Event | None = None
        self.interrupt_acknowledged = asyncio.Event()
        self.on_query: Callable[[str], None] | None = None
        self.query_error: Exception | None = None
        self.sent: list[dict[str, Any]] = []
        self.waiting: list[tuple[str | None, Any]] = []
        # meiva:note
        # A passive client only records what it is sent: the test replays the CLI's messages itself,
        # frame by frame, so the order of echoes and results is the recorded one.
        self.passive = False
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._turn: asyncio.Task[None] | None = None
        self._blocker = asyncio.Event()
        self._transport: SimpleNamespace | None = SimpleNamespace(
            _process=SimpleNamespace(pid=sdk.cli_pid)
        )

    async def connect(self) -> None:
        self.connected = True
        if self.options.fork_session and self.options.session_id in self._sdk.stored_sessions:
            raise RuntimeError(f"Session ID {self.options.session_id} is already in use.")
        reported = (
            self.options.session_id
            if self.options.fork_session and self.options.session_id
            else self.options.resume or self._sdk.new_session_id
        )
        self._sdk.stored_sessions.add(reported)
        self._emit(
            SystemMessage(
                subtype="init",
                data={
                    "session_id": reported,
                    "model": self.options.model,
                    "cwd": self.options.cwd,
                    "apiKeySource": "none",
                },
            )
        )

    async def disconnect(self) -> None:
        self.disconnected = True
        await self._cancel_turn()
        if self._sdk.probe is not None:
            self._sdk.probe.reap(self._sdk.cli_pid)
        self._queue.put_nowait(_END)

    def unblock(self) -> None:
        self._blocker.set()

    # meiva:note
    # The CLI's own input queue, as observed on CLI 2.1.301 with `replay-user-messages`: an input
    # that arrives while no turn runs starts one and is echoed first; an input that arrives while a
    # turn runs waits, is echoed (and so absorbed) at the next tool boundary, and when the turn ends
    # without one it is echoed after that turn's result and starts a turn of its own.
    # meiva:link base/lm/harness/claude/docs/claude.md
    async def query(self, prompt: Any) -> None:
        if self.query_error is not None:
            raise self.query_error
        messages: list[dict[str, Any]] = [{"uuid": None, "message": {"content": prompt}}]
        if not isinstance(prompt, str):
            messages = [message async for message in prompt]
        for message in messages:
            content: Any = message["message"]["content"]
            text = content if isinstance(content, str) else _joined_text(content)
            self.queries.append(text)
            self.sent.append(message)
            self.timeline.append(("query", text))
            if self.on_query is not None:
                self.on_query(text)
            if self.passive:
                continue
            entry: tuple[str | None, Any] = (message.get("uuid"), content)
            if self._turn is not None and not self._turn.done():
                self.waiting.append(entry)
            else:
                self._start_turn([entry])

    def _start_turn(self, echoes: list[tuple[str | None, Any]]) -> None:
        self._blocker = asyncio.Event()
        script = self._sdk.scripts.pop(0) if self._sdk.scripts else []
        self._turn = asyncio.create_task(self._run(script, echoes))

    def _echo(self, uuid: str | None, content: Any) -> None:
        self._emit(UserMessage(content=content, uuid=uuid))

    def _absorb_waiting(self) -> None:
        waiting, self.waiting = self.waiting, []
        for uuid, content in waiting:
            self._echo(uuid, content)

    def _revive_waiting(self) -> None:
        if self.waiting and self._turn is not None:
            echoes, self.waiting = self.waiting, []
            self._start_turn(echoes)

    async def _run(self, script: list[Any], echoes: list[tuple[str | None, Any]]) -> None:
        try:
            for uuid, content in echoes:
                self._echo(uuid, content)
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
                            ],
                            tool_use_result=step.structured,
                        )
                    )
                    self._absorb_waiting()
                elif isinstance(step, Block):
                    await self._blocker.wait()
                else:
                    raise TypeError(f"unknown script step: {step!r}")
        except asyncio.CancelledError:
            return
        self._finish(result=ResultMessage())
        self._revive_waiting()

    def _finish(self, *, result: ResultMessage) -> None:
        self.timeline.append(("result", result.subtype))
        self._emit(result)

    async def stop_task(self, task_id: str) -> None:
        self.stopped.append(task_id)
        self._emit(
            TaskUpdatedMessage(
                subtype="task_updated", data={}, task_id=task_id, patch={"status": "killed"}
            )
        )

    async def interrupt(self) -> None:
        self.interrupts += 1
        await self._cancel_turn()
        if self.on_interrupt is not None:
            self.on_interrupt()
        self._finish(
            result=ResultMessage(
                subtype="error_during_execution", is_error=True, terminal_reason="aborted_tools"
            )
        )
        if self.interrupt_ack_gate is not None:
            await self.interrupt_ack_gate.wait()
        self._revive_waiting()
        self.interrupt_acknowledged.set()

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

    # meiva:note
    # Deliver a message the CLI emitted outside any submitted turn — the real client's
    # notification stream, which a test drives directly instead of a background command.
    # meiva:link base/lm/harness/claude/docs/background-tasks.md
    def push(self, message: Any) -> None:
        self._emit(message)


class FakeSDK:
    def __init__(self, scripts: list[list[Any]] | None = None, *, cli_pid: int = 4242) -> None:
        self.scripts: list[list[Any]] = list(scripts or [])
        self.cli_pid = cli_pid
        # meiva:note
        # The real SDK terminates its CLI before `disconnect()` returns; a fake that
        # left it alive would make every close test wait out the adapter's settle
        # window and, worse, would stop modelling the one fact the close path relies on.
        # meiva:link base/lm/harness/docs/harness.md
        self.probe: ScriptedProbe | None = None
        self.new_session_id = "session-1"
        self.passive = False
        self.clients: list[FakeClaudeSDKClient] = []
        self.ClaudeAgentOptions = ClaudeAgentOptions
        # meiva:note
        # The sessions whose transcript exists. A session is stored once its init was reported, and the
        # real CLI refuses a fork under an id that is already stored, which this models.
        self.stored_sessions: set[str] = set()

    def get_session_info(self, session_id: str) -> object | None:
        return object() if session_id in self.stored_sessions else None

    def ClaudeSDKClient(self, options: ClaudeAgentOptions) -> FakeClaudeSDKClient:
        client = FakeClaudeSDKClient(options, sdk=self)
        client.passive = self.passive
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


def _joined_text(blocks: list[dict[str, Any]]) -> str:
    return " ".join(str(block.get("text", "")) for block in blocks if block.get("type") == "text")


def _delta(text: str) -> StreamEvent:
    return StreamEvent(
        event={
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }
    )
