from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from base.lm.harness.base_session import (
    EVENT_STREAM_END,
    BaseHarnessSession,
    InboundQueueFullError,
    SessionBinding,
    SessionClock,
    SessionClosedError,
    SessionTotals,
    TurnOutcome,
    TurnTicket,
)
from base.lm.harness.claude.auth import build_env
from base.lm.harness.claude.config import BridgeConfig
from base.lm.harness.claude.mapping import map_message, parse_result
from base.lm.harness.events import (
    AssistantText,
    SessionFaulted,
    SessionStarted,
    ToolUse,
    TurnCompleted,
)
from base.lm.harness.handoff import Handoff
from base.lm.harness.process import (
    ChildProbe,
    ProcessObservation,
    SdkClient,
    client_pid,
    client_process,
    describe,
    process_returncode,
    settle_close,
    snapshot,
    wait_gone,
    watch_tree,
)

# meiva:note
# One session is one turn engine with three boundaries. Inbound is synchronous and
# ordered: a message that arrives mid-turn waits for the running turn's
# `ResultMessage`, because a `query()` written earlier is folded into the pending
# tool result and the model treats it as injected content. Outbound is one event
# stream with one consumer; text deltas are droppable under lag, control events are
# not. Cancellation is a control message plus an assertion: `interrupt()` reports
# whether the tool's child processes were actually reaped, and `settled=None` when
# the CLI pid is unobservable — never a silent pass.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
CANCEL_POLL_SECONDS = 0.02
_EVENT_STREAM_END = EVENT_STREAM_END


class MissingClaudeSdkError(RuntimeError):
    pass


class ClaudeClientProtocol(SdkClient, Protocol):
    def connect(self) -> Awaitable[None]: ...
    def disconnect(self) -> Awaitable[None]: ...
    def query(self, prompt: str) -> Awaitable[None]: ...
    def interrupt(self) -> Awaitable[None]: ...
    def receive_messages(self) -> AsyncIterator[object]: ...


class ClaudeSdkProtocol(Protocol):
    @property
    def ClaudeAgentOptions(self) -> Callable[..., Any]:  # noqa: N802
        # meiva:note
        # Property name matches the `ClaudeAgentOptions` class exported by `claude_agent_sdk`.
        ...

    @property
    def ClaudeSDKClient(self) -> Callable[..., ClaudeClientProtocol]:  # noqa: N802
        # meiva:note
        # Property name matches the `ClaudeSDKClient` class exported by `claude_agent_sdk`.
        ...


def _load_sdk() -> ClaudeSdkProtocol:
    # meiva:note
    # The adapter extra is optional because the wheel bundles a ~240MB Claude Code
    # CLI that most machines never run. The import stays inside this function so
    # `import base.lm.harness` (the shared half) never touches it; a machine
    # without the extra still imports the package and fails only when it builds a
    # session, with the install command in the message.
    # meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
    try:
        import claude_agent_sdk
    except ImportError as exc:
        raise MissingClaudeSdkError(
            "claude-agent-sdk is not installed; install the adapter extra with "
            "pip install 'meiva[harness-claude]'"
        ) from exc
    return claude_agent_sdk


@dataclass(frozen=True, slots=True)
class CancelReport:
    turn_id: str | None
    interrupt_ms: float
    settled: bool | None
    observed: tuple[ProcessObservation, ...]
    remaining: tuple[ProcessObservation, ...]
    waited_ms: float
    cli_pid: int | None
    cli_alive: bool | None


class ClaudeSession(BaseHarnessSession[BridgeConfig]):
    def __init__(
        self,
        config: BridgeConfig,
        *,
        environ: Mapping[str, str],
        binding: SessionBinding,
        sdk: ClaudeSdkProtocol | None = None,
        probe: ChildProbe | None = None,
        clock: SessionClock,
    ) -> None:
        super().__init__(config, binding=binding, probe=probe, clock=clock)
        self._sdk: ClaudeSdkProtocol = sdk if sdk is not None else _load_sdk()
        self._environ = dict(environ)
        # meiva:note
        # The SDK resolves a bare `claude` with `shutil.which` against this process's own
        # PATH, and the bridge child this session runs in is handed an environment with
        # none (#5491). Pin the CLI we resolved; an injected SDK is a test double and must
        # not require the binary to exist.
        # meiva:link base/lm/harness/docs/harness.meiva.okf.md
        self._cli_path: str | None = None if sdk is not None else config.cli_path
        self._client: ClaudeClientProtocol | None = None
        self._cli_pid: int | None = None
        self._turn_steps_count = 0

    async def start(self, *, resume: str | None = None, handoff: Handoff | None = None) -> None:
        if self._client is not None:
            raise RuntimeError("session already started")
        if self._closed:
            raise SessionClosedError("session is closed")
        resume = self._resolve_resume(resume)
        client = self._sdk.ClaudeSDKClient(options=self._build_options(resume))
        await client.connect()
        self._client = client
        self._cli_pid = client_pid(client)
        await self._enter_tasks()
        self._reader = self._spawn(self._read_loop())
        self._inject_handoff(handoff)

    async def close(self, *, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        watched = watch_tree(self._probe, self._cli_pid)
        self._fail_pending(SessionClosedError("session closed"))
        await self._close_tasks()
        if self._client is not None:
            # meiva:note
            # The SDK releases its transport inside `disconnect()`, so hold the child
            # process now and ask it for its exit status afterwards (task #5616).
            child = client_process(self._client)
            await self._client.disconnect()
            self._exit_code = process_returncode(child)
        await settle_close(
            self._probe,
            watched,
            "claude_code",
            self.config.cancel_settle_seconds,
            agent_id=self.agent_id,
            clock=self._clock,
        )
        self._finish_close(reason)

    @property
    def cli_pid(self) -> int | None:
        return self._cli_pid

    async def interrupt(self) -> CancelReport:
        client = self._require_client()
        ticket = self._active
        if ticket is not None:
            ticket.interrupt_requested = True
        pid = self._cli_pid
        observed = snapshot(self._probe, pid)
        spawned = {
            child: command for child, command in observed.items() if child not in self._baseline
        }
        started = self._monotonic()
        await client.interrupt()
        interrupt_ms = (self._monotonic() - started) * 1000.0
        if pid is None:
            return CancelReport(
                turn_id=ticket.turn_id if ticket is not None else None,
                interrupt_ms=interrupt_ms,
                settled=None,
                observed=(),
                remaining=(),
                waited_ms=0.0,
                cli_pid=None,
                cli_alive=None,
            )
        gone, waited_ms = await wait_gone(
            self._probe,
            spawned,
            timeout_seconds=self.config.cancel_settle_seconds,
            poll_seconds=CANCEL_POLL_SECONDS,
            clock=self._clock,
        )
        remaining = (
            ()
            if gone
            else describe(observed, [pid for pid in spawned if self._probe.is_alive(pid)])
        )
        return CancelReport(
            turn_id=ticket.turn_id if ticket is not None else None,
            interrupt_ms=interrupt_ms,
            settled=gone,
            observed=describe(observed, spawned),
            remaining=remaining,
            waited_ms=waited_ms,
            cli_pid=pid,
            cli_alive=self._probe.is_alive(pid),
        )

    def _build_options(self, resume: str | None) -> object:
        # meiva:note
        # `cli_path` and `thinking` ride only when resolved/supported: Python's own
        # Claude Code SDK takes them, while a test double that models the older
        # option set would reject them.
        # meiva:link base/lm/harness/docs/harness.meiva.okf.md
        resolved: dict[str, Any] = {} if self._cli_path is None else {"cli_path": self._cli_path}
        opt_fields = getattr(self._sdk.ClaudeAgentOptions, "__dataclass_fields__", {})
        if "thinking" in opt_fields:
            resolved["thinking"] = {"type": "adaptive", "display": "summarized"}
        return self._sdk.ClaudeAgentOptions(
            **resolved,
            model=self.config.model,
            cwd=str(self.config.cwd),
            tools=None if self.config.tools is None else list(self.config.tools),
            permission_mode=self.config.permission_mode,
            max_turns=self.config.max_turns,
            setting_sources=[],
            strict_mcp_config=True,
            include_partial_messages=True,
            env=build_env(self._environ, self.config.auth, host=self._environ),
            resume=resume,
        )

    async def _begin(self, ticket: TurnTicket) -> None:
        client = self._require_client()
        ticket.started_at = self._monotonic()
        self._turn_steps_count = 0
        self._baseline = snapshot(self._probe, self._cli_pid)
        self._start_deadline(ticket)
        try:
            await client.query(ticket.text)
        except Exception as exc:
            self._fail_turn(ticket, exc)

    async def _read_loop(self) -> None:
        client = self._require_client()
        try:
            async for message in client.receive_messages():
                self._handle(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(SessionFaulted(turn_id=None, error=f"session stream failed: {exc}"))
            self._fail_active(exc)

    def _handle(self, message: object) -> None:
        name = type(message).__name__
        if name == "ResultMessage":
            self._handle_result(message)
            return
        if name in {"RateLimitEvent", "SystemMessage"}:
            for event in map_message(message, turn_id=None):
                if isinstance(event, SessionStarted):
                    self._session_id = event.session_id or self._session_id
                    self._model = event.model or self._model
                    self._emit(event)
                    self._bind()
                    continue
                self._emit(event)
            return
        ticket = self._active
        if ticket is None:
            return
        mapped = map_message(message, turn_id=ticket.turn_id)
        if name == "AssistantMessage" and any(
            isinstance(event, (AssistantText, ToolUse)) for event in mapped
        ):
            self._turn_steps_count += 1
        for event in mapped:
            if isinstance(event, AssistantText):
                ticket.parts.append(event.text)
            self._emit(event)

    def _handle_result(self, message: object) -> None:
        ticket = self._active
        fields = parse_result(message)
        if ticket is None:
            self._emit(
                SessionFaulted(
                    turn_id=None,
                    error=f"result for no active turn (subtype={fields.subtype})",
                )
            )
            return
        text = "\n".join(ticket.parts).strip() or (fields.result_text or "")
        outcome = TurnOutcome(
            turn_id=ticket.turn_id,
            text=text,
            subtype=fields.subtype,
            is_error=fields.is_error,
            interrupted=ticket.interrupt_requested,
            stop_reason=fields.stop_reason,
            usage=fields.usage,
            cost_usd=fields.cost_usd,
            permission_denials=fields.permission_denials,
        )
        self._totals = self._totals.with_turn(fields.usage, fields.cost_usd)
        if fields.session_id:
            self._session_id = fields.session_id
        num_turns = max(fields.num_turns, self._turn_steps_count, 1)
        self._emit(
            TurnCompleted(
                turn_id=ticket.turn_id,
                session_id=fields.session_id or self._session_id,
                text=text,
                subtype=fields.subtype,
                is_error=fields.is_error,
                interrupted=ticket.interrupt_requested,
                stop_reason=fields.stop_reason,
                num_turns=num_turns,
                duration_ms=fields.duration_ms,
                usage=fields.usage,
                cost_usd=fields.cost_usd,
                cumulative_cost_usd=self._totals.cost_usd,
                permission_denials=fields.permission_denials,
            )
        )
        self._complete(ticket, outcome)

    def _require_client(self) -> ClaudeClientProtocol:
        if self._client is None:
            raise SessionClosedError("session is not started")
        return self._client


__all__ = [
    "CANCEL_POLL_SECONDS",
    "EVENT_STREAM_END",
    "CancelReport",
    "ClaudeSession",
    "InboundQueueFullError",
    "MissingClaudeSdkError",
    "SessionClosedError",
    "SessionTotals",
    "TurnOutcome",
    "TurnTicket",
]
