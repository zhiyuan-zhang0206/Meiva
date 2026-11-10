from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Protocol, assert_never

import claude_agent_sdk

from base.host.boundary.json_values import is_object_dict
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
from base.lm.harness.claude.delivery import (
    OpenTurn,
    harness_cause,
    key_uuid,
    user_message,
)
from base.lm.harness.claude.inbound import (
    BackgroundTasks,
    MessageKind,
    assistant_texts,
    counts_as_step,
    is_tool_result,
    message_kind,
    notification_plan,
    started_identity,
)
from base.lm.harness.claude.mapping import map_message, parse_result, task_update_status
from base.lm.harness.claude.options import fork_session_id, option_values
from base.lm.harness.claude.settle import RunningTurn, settle_result
from base.lm.harness.delivery import HarnessInput, InputPhase, validated_batch
from base.lm.harness.events import (
    AckSignal,
    ForegroundAccepted,
    HarnessCause,
    InterruptConfirmed,
    SessionFailed,
    SessionStarted,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
)
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
from base.lm.harness.session import TurnOutcome

# meiva:note
# One session is one turn engine with three boundaries. Inbound is `deliver()`: every input is
# written with its own uuid and the CLI's echo of that uuid is the proof the model has it. Turns are
# observed from echoes and `ResultMessage`s, not from our submissions (see `delivery.py`), so a turn the
# CLI starts on its own is a turn like any other. Ordinary `submit()` still waits for its turn's
# result. Outbound is one event stream with one consumer; text deltas are droppable under lag,
# control events are not. Cancellation is a control message plus an assertion:
# `interrupt()` reports whether the tool's child processes were actually reaped, and
# `settled=None` when the CLI pid is unobservable — never a silent pass.
# meiva:link base/lm/harness/claude/docs/claude.md
CANCEL_POLL_SECONDS = 0.02
_EVENT_STREAM_END = EVENT_STREAM_END


class ClaudeClientProtocol(SdkClient, Protocol):
    def connect(self) -> Awaitable[None]: ...
    def disconnect(self) -> Awaitable[None]: ...
    def query(self, prompt: str | AsyncIterator[dict[str, object]]) -> Awaitable[None]: ...
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

    @property
    def get_session_info(self) -> Callable[[str], object | None]:
        # meiva:note
        # The SDK's lookup of one stored session by id; it answers None for a session whose
        # transcript does not exist or is empty.
        ...


async def _one_message(message: dict[str, object]) -> AsyncIterator[dict[str, object]]:
    yield message


def _origin_kind(message: object) -> str | None:
    origin: object = getattr(message, "origin", None)
    if not is_object_dict(origin):
        return None
    value = origin.get("kind")
    return value if isinstance(value, str) else None


def _task_id(message: object) -> str:
    return str(getattr(message, "task_id", ""))


class ClaudeSession(BaseHarnessSession[BridgeConfig]):
    harness = "claude_code"

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
        # meiva:link base/lm/harness/docs/harness.md
        self._cli_path: str | None = None if sdk is not None else config.cli_path
        self._client: ClaudeClientProtocol | None = None
        self._cli_pid: int | None = None
        # meiva:note
        # The tasks the CLI says are running: started and not yet reported terminal.
        # `close()` is their one stop caller, and the note there says why.
        self._background = BackgroundTasks()
        self._open: OpenTurn | None = None
        self._uuid_keys: dict[str, str] = {}
        self._echo_seen = False
        self._pending_cause: HarnessCause | None = None

    async def start(self, *, resume: str | None = None, handoff: Handoff | None = None) -> None:
        if self._client is not None:
            raise RuntimeError("session already started")
        if self._closed:
            raise SessionClosedError("session is closed")
        resume = self._resolve_resume(resume)
        _ = self._begin_generation()
        fork_to: str | None = None
        origin = self._fork_origin(resume)
        if origin is not None:
            clone = fork_session_id(self._require_agent_id())
            if self._sdk.get_session_info(clone) is not None:
                resume = clone
            else:
                resume, fork_to = origin.session_id, clone
        client = self._sdk.ClaudeSDKClient(options=self._build_options(resume, fork_to))
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
        # meiva:link base/lm/harness/claude/docs/claude.md
        client, live = self._client, list(self._background.open_task_ids)
        self._background = BackgroundTasks()
        self._open = None
        self._observed.clear()
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
    # Whether a turn of ours is running and still accepts steering through the compatibility
    # wrapper (`steer()` in the base session, a thin layer over `deliver()`). `deliver()` itself
    # is always available: the CLI queues what it is handed, whatever it is doing.
    # meiva:link base/lm/harness/claude/docs/claude.md
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

    # meiva:note
    # Hand a batch to the CLI, in order, each with its own uuid. A successful write is a weak receipt
    # (`InputReceived`, WEAK): the CLI may still hold the message in its queue, and it reaches the model
    # at the next tool boundary or, when the turn has no tool left to cross, as a turn of its own after
    # this one ends. The echo is what says which happened. Nothing is refused: the CLI accepts input
    # in any state, including after an interrupt, where a queued input revives as a new turn.
    # meiva:link base/lm/harness/docs/delivery.md
    async def deliver(self, inputs: Sequence[HarnessInput]) -> None:
        batch = validated_batch(inputs)
        _ = self._require_client()
        for entry in batch:
            await self._send(entry)

    # meiva:note
    # What an input was written behind: the turn that was open, or the inputs still unsettled when no
    # turn has opened yet (a write that lands right behind the write that starts a turn). Nothing in
    # front of it means the input starts the turn itself; anything in front means that if the CLI does
    # not absorb it, it revives as a turn of its own.
    def _send_scope(self) -> str | None:
        if self._open is not None:
            return self._open.turn_id
        unsettled = (
            *self._ledger.keys_in(InputPhase.SENT),
            *self._ledger.keys_in(InputPhase.RECEIVED),
        )
        return "unsettled-inputs" if unsettled else None

    async def _send(self, entry: HarnessInput) -> None:
        client = self._require_client()
        self._ledger = self._ledger.sent(entry.key, scope=self._send_scope())
        self._uuid_keys[key_uuid(entry.key)] = entry.key
        try:
            await client.query(_one_message(user_message(entry)))
        except Exception:
            self._apply(self._ledger.rejected(entry.key, "the write to the CLI failed"))
            raise
        self._apply(self._ledger.received(entry.key, AckSignal.WEAK))

    async def interrupt(self, *, turn_id: str | None = None) -> CancelReport:
        ticket = await self._interrupt_target(turn_id)
        if ticket is None and not self._observed_turn_named(turn_id):
            return unobserved_cancel(turn_id=turn_id)
        client = self._require_client()
        pid = self._cli_pid
        turn_id = ticket.turn_id if ticket is not None else self._open_id()
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
    async def _interrupt_control(
        self, client: ClaudeClientProtocol, ticket: TurnTicket | None
    ) -> None:
        errors: list[Exception] = []

        async def send() -> None:
            try:
                if ticket is None or self._active is ticket:
                    await client.interrupt()
            except Exception as exc:
                if ticket is not None:
                    ticket.interrupt_requested = False
                errors.append(exc)

        await asyncio.shield(self._spawn(send()))
        if errors:
            raise errors[0]

    def _require_agent_id(self) -> int:
        if self.agent_id is None:
            raise RuntimeError("a fork session needs the agent that owns it")
        return self.agent_id

    def _build_options(self, resume: str | None, fork_to: str | None = None) -> object:
        options = self._sdk.ClaudeAgentOptions
        return options(
            **option_values(
                self.config,
                environ=self._environ,
                cli_path=self._cli_path,
                option_fields=getattr(options, "__dataclass_fields__", {}),
                resume=resume,
                fork_to=fork_to,
            )
        )

    async def _begin(self, ticket: TurnTicket) -> None:
        ticket.started_at = self._monotonic()
        self._baseline = snapshot(self._probe, self._cli_pid)
        entry = HarnessInput(key=ticket.input_key, text=ticket.text, images=ticket.images)
        try:
            await self._send(entry)
            ticket.interrupt_ready.set()
            self._emit(ForegroundAccepted(turn_id=ticket.turn_id))
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
    # meiva:link base/lm/harness/claude/docs/background-tasks.md
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

    # meiva:note
    # Everything inside a turn is attributed to the turn the echoes and results say is open. A user
    # message is either a tool result (content of the open turn) or an echo of an input: an echo of
    # one of our uuids opens a turn or joins the open one, an echo that is not ours is ignored unless
    # it names why the CLI began a turn. Content with no turn open means the CLI began one itself.
    # meiva:link base/lm/harness/docs/delivery.md
    def _handle_turn_message(self, message: object) -> None:
        if type(message).__name__ == "UserMessage" and not is_tool_result(message):
            self._handle_echo(message)
            return
        turn = self._open or self._open_harness_turn(None)
        mapped = map_message(message, turn_id=turn.turn_id)
        self._open = turn.with_parts(
            assistant_texts(mapped), stepped=counts_as_step(type(message).__name__, mapped)
        )
        for event in mapped:
            self._emit(event)

    def _handle_echo(self, message: object) -> None:
        key = self._uuid_keys.get(str(getattr(message, "uuid", "") or ""))
        if key is None:
            origin = _origin_kind(message)
            if self._open is None and origin not in (None, "human"):
                _ = self._open_harness_turn(origin)
            return
        self._echo_seen = True
        if self._open is None:
            self._open_input_turn(key)
        else:
            self._absorb(key)
        if self._open is not None:
            self._apply(self._ledger.in_context(key, self._open.turn_id))

    def _absorb(self, key: str) -> None:
        turn = self._open
        if turn is None:
            return
        ticket = self._active
        mine = ticket is not None and ticket.input_key == key
        if mine and ticket is not None and not turn.owner_ticket:
            turn = turn.with_alias(ticket.turn_id)
        self._open = turn.absorbing(key)

    # meiva:note
    # An echo of our input with no turn open begins a turn. If the input is the one our active ticket
    # sent, the turn is the ticket's. If it was written while another turn ran and that turn ended
    # without taking it, the CLI revived it as a turn of its own: that is a harness turn
    # (QUEUED_INPUT_REVIVED) that names the key. Otherwise the input started the turn.
    def _open_input_turn(self, key: str) -> None:
        ticket = self._active
        if ticket is not None and ticket.input_key == key:
            self._open = OpenTurn(ticket.turn_id, (key,), owner_ticket=True)
            self._emit(TurnStarted(ticket.turn_id, TurnOrigin.INPUT, (key,), self.generation))
            return
        turn_id = self._mint_turn_id()
        self._open = OpenTurn(turn_id, (key,))
        self._observed.add(turn_id)
        revived = self._ledger.scope_of(key) is not None
        self._emit(
            TurnStarted(
                turn_id,
                TurnOrigin.HARNESS if revived else TurnOrigin.INPUT,
                (key,),
                self.generation,
                cause=HarnessCause.QUEUED_INPUT_REVIVED if revived else None,
            )
        )

    def _open_harness_turn(self, origin: str | None) -> OpenTurn:
        turn_id = self._mint_turn_id()
        cause = self._pending_cause or harness_cause(origin)
        self._pending_cause = None
        self._open = OpenTurn(turn_id)
        self._observed.add(turn_id)
        self._emit(
            TurnStarted(
                turn_id, TurnOrigin.HARNESS, (), self.generation, cause=cause, detail=origin
            )
        )
        return self._open

    def _open_id(self) -> str | None:
        return self._open.turn_id if self._open is not None else None

    def _observed_turn_named(self, turn_id: str | None) -> bool:
        turn = self._open
        if turn is None or turn.owner_ticket:
            return False
        return turn_id is None or turn_id == turn.turn_id

    def _handle_task_notification(self, message: object) -> None:
        self._background = self._background.reported(_task_id(message))
        plan = notification_plan(map_message(message, turn_id=None), claimed=self._open is not None)
        for event in plan.stream:
            self._emit(event)
        if plan.report is not None:
            self._pending_cause = HarnessCause.BACKGROUND_TASK
            self._hold_dispatch(self.config.injected_dispatch_wait_seconds)
            self._unsolicited(plan.report)

    def _track_task_started(self, message: object) -> None:
        self._background = self._background.started(_task_id(message))

    def _track_task_update(self, message: object) -> None:
        self._background = self._background.updated(_task_id(message), task_update_status(message))

    # meiva:note
    # A result ends the open turn, whoever began it: a turn of ours settles its ticket, a turn the CLI
    # began (a background-task answer, a revived input, a timer) completes as an observed turn and the
    # next queued submission goes after it. A result with no turn open still completes a turn: the
    # CLI ran one that produced no content. A result that arrives when no echo was ever seen, with an
    # input in flight, means `replay-user-messages` is not honoured, and without it nothing can be
    # attributed, so the session fails instead of guessing.
    # meiva:link base/lm/harness/docs/delivery.md
    def _handle_result(self, message: object) -> None:
        fields = parse_result(message)
        if not self._echo_seen and self._ledger.keys_in(InputPhase.RECEIVED):
            error = "the CLI did not echo our user message; replay-user-messages is not available"
            self._emit(SessionFailed(error=error))
            self._fail_active(SessionClosedError(error))
            return
        turn = self._open or self._open_harness_turn(fields.origin_kind)
        settlement = settle_result(
            fields,
            turn=RunningTurn(turn_id=turn.turn_id, parts=turn.parts, steps=turn.steps),
            session_id=self._session_id,
            totals=self._totals,
        )
        self._totals = settlement.totals
        self._session_id = settlement.session_id
        completed = replace(settlement.completed, generation=self.generation)
        self._open = None
        self._emit(completed)
        if completed.interrupted:
            self._emit(InterruptConfirmed(turn.turn_id, self.generation))
        self._finish_turn(turn, settlement.outcome, completed)

    def _finish_turn(self, turn: OpenTurn, outcome: TurnOutcome, completed: TurnCompleted) -> None:
        ticket = self._active
        for alias in turn.aliases:
            if ticket is not None and ticket.turn_id == alias:
                self._emit(replace(completed, turn_id=alias))
                self._complete(ticket, replace(outcome, turn_id=alias))
                ticket = None
        if turn.owner_ticket and ticket is not None:
            self._complete(ticket, outcome)
            return
        if not turn.owner_ticket:
            self._observed.discard(turn.turn_id)
            self._release_dispatch(keep_events=True)
            self._promote(keep_events=True)

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
