from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Protocol, assert_never

import claude_agent_sdk

from base.lm.harness.base_session import (
    EVENT_STREAM_END,
    BaseHarnessSession,
    InboundQueueFullError,
    SessionBinding,
    SessionClock,
    SessionClosedError,
    TurnTicket,
)
from base.lm.harness.cancel import (
    CancelReport,
    Sweep,
    observed_cancel,
    spawned_since,
    unobserved_cancel,
)
from base.lm.harness.claude.config import BridgeConfig
from base.lm.harness.claude.inbound import (
    BackgroundTasks,
    MessageKind,
    assistant_texts,
    claimed_origin,
    counts_as_step,
    message_kind,
    notification_plan,
    started_identity,
)
from base.lm.harness.claude.mapping import map_message, parse_result, task_update_status
from base.lm.harness.claude.options import option_values
from base.lm.harness.claude.settle import RunningTurn, result_output, settle_result
from base.lm.harness.events import SessionFailed, SessionStarted
from base.lm.harness.handoff import Handoff
from base.lm.harness.process import (
    ChildProbe,
    SdkClient,
    client_pid,
    client_process,
    process_returncode,
    settle_close,
    snapshot,
    wait_gone,
    watch_tree,
)

# meiva:note
# One session is one turn engine with three boundaries. Inbound is synchronous and
# ordered for submitted turns: ordinary `submit()` waits for the running turn's
# `ResultMessage`. Mid-turn steering (`ForegroundSteering`) writes directly to the
# active session via `query()`, which the model receives in its ongoing turn context.
# Outbound is one event stream with one consumer; text deltas are droppable under lag,
# control events are not. Cancellation is a control message plus an assertion:
# `interrupt()` reports whether the tool's child processes were actually reaped, and
# `settled=None` when the CLI pid is unobservable — never a silent pass.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
CANCEL_POLL_SECONDS = 0.02
_EVENT_STREAM_END = EVENT_STREAM_END


class ClaudeClientProtocol(SdkClient, Protocol):
    def connect(self) -> Awaitable[None]: ...
    def disconnect(self) -> Awaitable[None]: ...
    def query(self, prompt: str) -> Awaitable[None]: ...
    def interrupt(self) -> Awaitable[None]: ...
    def stop_task(self, task_id: str) -> Awaitable[None]: ...
    def receive_messages(self) -> AsyncIterator[object]: ...


class ClaudeSdkProtocol(Protocol):
    @property
    def ClaudeAgentOptions(self) -> Callable[..., object]:  # noqa: N802
        # meiva:note
        # Property name matches the `ClaudeAgentOptions` class exported by `claude_agent_sdk`.
        ...

    @property
    def ClaudeSDKClient(self) -> Callable[..., ClaudeClientProtocol]:  # noqa: N802
        # meiva:note
        # Property name matches the `ClaudeSDKClient` class exported by `claude_agent_sdk`.
        ...


def _task_id(message: object) -> str:
    return str(getattr(message, "task_id", ""))


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
        self._sdk: ClaudeSdkProtocol = sdk if sdk is not None else claude_agent_sdk
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
        # meiva:note
        # The tasks the CLI says are running: started and not yet reported terminal.
        # `close()` is their one stop caller, and the note there says why.
        self._background = BackgroundTasks()
        self._pending_steers: set[tuple[str, str]] = set()
        self._steered_keys: set[tuple[str, str]] = set()

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
        # meiva:note
        # A background task outlives the turn that started it by design, so nothing in
        # `interrupt()` touches it — and a closing session is the moment nobody will ever
        # read its report. The CLI dies under its running children when `disconnect()`
        # runs below, and `settle_close` would then report them as residue; asking the
        # CLI to stop each one first turns that teardown into a provider-managed stop. A
        # stop that fails is not the close's failure: the residue check that follows is
        # what reports whatever kept running.
        # meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
        client, live = self._client, list(self._background.open_task_ids)
        self._background = BackgroundTasks()
        self._pending_steers.clear()
        self._steered_keys.clear()
        if client is not None and live:
            await asyncio.gather(
                *(client.stop_task(task_id) for task_id in live), return_exceptions=True
            )
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

    # meiva:note
    # Foreground steering writes mid-turn input directly into the running CLI session
    # via `query()`, relaxing the RPC-ACK requirement to transport write confirmation.
    # It is available while an active turn is running and not interrupted. Ordinary
    # `submit()` continues to queue one turn at a time.
    # meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
    @property
    def steering_available(self) -> bool:
        ticket = self._active
        return (
            not self._closed
            and self._client is not None
            and ticket is not None
            and not ticket.interrupt_requested
            and ticket.interrupt_ready.is_set()
        )

    async def steer(self, text: str, *, key: str) -> bool:
        if not text.strip() or not key:
            raise ValueError("steering requires nonempty text and a message key")
        if not self.steering_available:
            return False
        ticket = self._active
        client = self._client
        if ticket is None or client is None:
            return False
        turn_id = ticket.turn_id
        identity = (turn_id, key)
        if identity in self._pending_steers:
            raise RuntimeError("steer request already pending for this message key")
        if identity in self._steered_keys or (ticket.key is not None and key == ticket.key):
            return True
        self._pending_steers.add(identity)
        try:
            await client.query(text)
            self._steered_keys.add(identity)
            return True
        finally:
            self._pending_steers.discard(identity)

    async def interrupt(self, *, turn_id: str | None = None) -> CancelReport:
        ticket = await self._interrupt_target(turn_id)
        if ticket is None:
            return unobserved_cancel(turn_id=turn_id)
        client = self._require_client()
        pid = self._cli_pid
        turn_id = ticket.turn_id
        observed = snapshot(self._probe, pid)
        spawned = spawned_since(observed, self._baseline)
        started = self._monotonic()
        await self._interrupt_control(client, ticket)
        interrupt_ms = (self._monotonic() - started) * 1000.0
        if pid is None:
            return unobserved_cancel(turn_id=turn_id, interrupt_ms=interrupt_ms)
        gone, waited_ms = await wait_gone(
            self._probe,
            spawned,
            timeout_seconds=self.config.cancel_settle_seconds,
            poll_seconds=CANCEL_POLL_SECONDS,
            clock=self._clock,
        )
        return observed_cancel(
            turn_id=turn_id,
            interrupt_ms=interrupt_ms,
            pid=pid,
            cli_alive=self._probe.is_alive(pid),
            sweep=Sweep(
                observed=observed,
                spawned=spawned,
                gone=gone,
                waited_ms=waited_ms,
                alive=() if gone else [child for child in spawned if self._probe.is_alive(child)],
            ),
        )

    # meiva:note
    # The result may reach the bridge before the SDK sees its control reply.
    # Let the session finish that exchange so the SDK can clear its request
    # bookkeeping even when the foreground no longer needs to await the ACK.
    async def _interrupt_control(self, client: ClaudeClientProtocol, ticket: TurnTicket) -> None:
        errors: list[Exception] = []

        async def send() -> None:
            try:
                if self._active is ticket:
                    await client.interrupt()
            except Exception as exc:
                ticket.interrupt_requested = False
                errors.append(exc)

        await asyncio.shield(self._spawn(send()))
        if errors:
            raise errors[0]

    def _build_options(self, resume: str | None) -> object:
        options = self._sdk.ClaudeAgentOptions
        return options(
            **option_values(
                self.config,
                environ=self._environ,
                cli_path=self._cli_path,
                option_fields=getattr(options, "__dataclass_fields__", {}),
                resume=resume,
            )
        )

    async def _begin(self, ticket: TurnTicket) -> None:
        client = self._require_client()
        ticket.started_at = self._monotonic()
        self._turn_steps_count = 0
        self._baseline = snapshot(self._probe, self._cli_pid)
        try:
            await client.query(ticket.text)
            ticket.interrupt_ready.set()
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
            self._emit(SessionFailed(error=f"session stream failed: {exc}"))
            self._fail_active(exc)
        else:
            error = SessionClosedError("Claude event stream ended")
            self._emit(SessionFailed(error=str(error)))
            self._fail_active(error)

    # meiva:note
    # One dispatch table, keyed by the provider's own class name: a result is a turn
    # boundary, the task messages are reports about work outside the turn, the session
    # messages are about the session, and everything else is inside the running turn.
    # meiva:link base/lm/harness/claude/docs/background-tasks.meiva.okf.md
    def _handle(self, message: object) -> None:
        kind = message_kind(type(message).__name__)
        match kind:
            case MessageKind.RESULT:
                self._handle_result(message)
            case MessageKind.TASK_NOTIFICATION:
                self._handle_task_notification(message)
            case MessageKind.TASK_STARTED:
                self._track_task_started(message)
            case MessageKind.TASK_UPDATED:
                self._track_task_update(message)
            case MessageKind.SESSION:
                self._handle_session_message(message)
            case MessageKind.TURN:
                self._handle_turn_message(message)
            case _:
                assert_never(kind)

    def _handle_session_message(self, message: object) -> None:
        for event in map_message(message, turn_id=None):
            if isinstance(event, SessionStarted):
                identity = started_identity(event, session_id=self._session_id, model=self._model)
                self._session_id = identity.session_id
                self._model = identity.model
                self._emit(event)
                self._bind()
                continue
            self._emit(event)

    def _handle_turn_message(self, message: object) -> None:
        ticket = self._active
        if ticket is None:
            return
        mapped = map_message(message, turn_id=ticket.turn_id)
        if counts_as_step(type(message).__name__, mapped):
            self._turn_steps_count += 1
        ticket.parts.extend(assistant_texts(mapped))
        for event in mapped:
            self._emit(event)

    def _handle_task_notification(self, message: object) -> None:
        self._background = self._background.reported(_task_id(message))
        plan = notification_plan(
            map_message(message, turn_id=None), claimed=self._claim() is not None
        )
        for event in plan.stream:
            self._emit(event)
        if plan.report is not None:
            self._hold_dispatch(self.config.injected_dispatch_wait_seconds)
            self._unsolicited(plan.report)

    def _track_task_started(self, message: object) -> None:
        self._background = self._background.started(_task_id(message))

    def _track_task_update(self, message: object) -> None:
        self._background = self._background.updated(_task_id(message), task_update_status(message))

    def _handle_result(self, message: object) -> None:
        fields = parse_result(message)
        ticket = self._claim(claimed_origin(fields.origin_kind))
        if ticket is None:
            self._emit(result_output(fields))
            self._release_dispatch()
            return
        self._steered_keys = {item for item in self._steered_keys if item[0] != ticket.turn_id}
        settlement = settle_result(
            fields,
            turn=RunningTurn(
                turn_id=ticket.turn_id,
                parts=tuple(ticket.parts),
                steps=self._turn_steps_count,
            ),
            session_id=self._session_id,
            totals=self._totals,
        )
        self._totals = settlement.totals
        self._session_id = settlement.session_id
        self._emit(settlement.completed)
        self._complete(ticket, settlement.outcome)

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
    "SessionClosedError",
    "TurnTicket",
]
