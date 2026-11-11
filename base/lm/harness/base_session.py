from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine, Sequence
from dataclasses import dataclass, replace
from types import TracebackType
from typing import ClassVar, Self

from base.lm.harness.delivery import DeliveryLedger, HarnessInput, ImageBlock, LedgerStep
from base.lm.harness.events import (
    BridgeEvent,
    SessionBound,
    SessionClosed,
    SessionFailed,
    TurnFaulted,
    UnsolicitedOutput,
    UnsolicitedSink,
    is_droppable,
)
from base.lm.harness.process import ChildProbe, ExitStatus, PsutilChildProbe
from base.lm.harness.registry import SessionRecord, SessionRegistry
from base.lm.harness.session import (
    EVENT_STREAM_END,
    SessionClock,
    SessionConfig,
    SessionTotals,
)

# meiva:note
# Shared turn-engine state machine for every external harness session adapter:
# ordered inbound turn promotion, key-based idempotency replay, bounded outbound
# delta dropping, background task-group lifecycle,
# and `SessionRegistry` binding/resumption.
# meiva:link base/lm/harness/docs/harness.md


class ForkSourceUnavailableError(RuntimeError):
    pass


# meiva:note
# The turn an input of ours started in an idle session, as far as the session needs to know it: its local
# id, the provider's name for it once known, whether an interrupt is under way and whether one can be sent yet.
# It carries no outcome and no queue: a turn is reported only by the events it produces, and what to
# deliver next is the caller's decision.
# meiva:link base/lm/harness/docs/harness.md
class ActiveTurn:
    def __init__(
        self, *, turn_id: str, text: str, key: str, images: tuple[ImageBlock, ...] = ()
    ) -> None:
        self.turn_id = turn_id
        self.text = text
        self.input_key = key
        self.images = images
        self.started_at: float | None = None
        self.provider_turn_id: str | None = None
        self.interrupt_requested = False
        self.interrupt_ready = asyncio.Event()
        self.parts: list[str] = []
        self.thoughts: list[str] = []
        self.denials: list[str] = []


# meiva:note
# What one session is bound to: the agent it serves, the registry that maps that agent to its
# external session id, and the sink that turns provider output the agent has not read into
# something delivered. All three are absent for a session no agent owns (a demo, a unit test).
# The sink rides here rather than as another constructor parameter because it answers the same
# question the other two do — where this session's output goes — and because every adapter
# builds one binding already, so an adapter that starts mapping unowned output needs no
# signature change (#5663).
# meiva:link base/lm/harness/docs/harness.md
@dataclass(frozen=True)
class SessionBinding:
    agent_id: int | None = None
    registry: SessionRegistry | None = None
    unsolicited: UnsolicitedSink | None = None
    fork_source_agent_id: int | None = None


class BaseHarnessSession[ConfigT: SessionConfig](ExitStatus):
    # meiva:note
    # The harness this adapter drives: the registry keeps one record per (agent, harness), and
    # the adapter reads and writes only its own.
    # meiva:link base/lm/harness/docs/session-identity.md
    harness: ClassVar[str]

    def __init__(
        self,
        config: ConfigT,
        *,
        binding: SessionBinding,
        probe: ChildProbe | None = None,
        totals: SessionTotals | None = None,
        clock: SessionClock,
    ) -> None:
        self.config: ConfigT = config
        self.agent_id: int | None = binding.agent_id
        self._probe: ChildProbe = probe or PsutilChildProbe()
        self._registry: SessionRegistry | None = binding.registry
        self._clock: SessionClock = clock
        self._events: asyncio.Queue[BridgeEvent | object] = asyncio.Queue()
        self._active: ActiveTurn | None = None
        self._reader: asyncio.Task[None] | None = None
        self._tasks = asyncio.TaskGroup()
        self._children: set[asyncio.Task[None]] = set()
        self._tasks_entered = False
        self._totals: SessionTotals = totals if totals is not None else SessionTotals()
        self._session_id: str | None = None
        self._model: str | None = config.model
        self._baseline: dict[int, str] = {}
        self._closed = False
        self._dropped_deltas = 0
        self._resumed_existing = False
        self._record: SessionRecord | None = None
        self._turn_seq = 0
        self._unsolicited_sink = binding.unsolicited
        self._fork_source_agent_id = binding.fork_source_agent_id
        self._dispatch_held = False
        self._release_timer: asyncio.Task[None] | None = None
        self._unconsumed_events = 0
        self._ledger = DeliveryLedger()
        self._observed: set[str] = set()

    async def start(self, *, resume: str | None = None) -> None:
        raise NotImplementedError

    async def close(self, *, reason: str = "closed") -> None:
        raise NotImplementedError

    async def interrupt(self, *, turn_id: str | None = None) -> object:
        raise NotImplementedError

    # meiva:note
    # The one delivery operation: hand a batch of inputs to the foreground, in order. It returns when
    # the batch has been handed over, not when it was accepted: every result comes back as an event
    # (`InputReceived`, `InputInContext`, `InputRejected`, `InputLost`). Whether an input joins the
    # running turn or begins a new one is the adapter's decision, not the caller's.
    # meiva:link base/lm/harness/docs/delivery.md
    async def deliver(self, inputs: Sequence[HarnessInput]) -> None:
        raise NotImplementedError

    @property
    def generation(self) -> int:
        return self._ledger.generation

    def _mint_turn_id(self) -> str:
        self._turn_seq += 1
        return f"turn-{self._turn_seq}"

    def _begin_generation(self) -> int:
        self._ledger = self._ledger.next_generation()
        return self._ledger.generation

    def _apply(self, step: LedgerStep) -> None:
        self._ledger = step.ledger
        for event in step.events:
            self._emit(event)

    # meiva:note
    # Startup and completion can both race a cancel. Wait on the captured ticket
    # for the adapter to finish addressing the prompt, then recheck ownership.
    async def _interrupt_target(self, turn_id: str | None) -> ActiveTurn | None:
        turn = self._active
        if turn is None or (turn_id is not None and turn.turn_id != turn_id):
            return None
        await turn.interrupt_ready.wait()
        if self._active is not turn or turn.interrupt_requested:
            return None
        turn.interrupt_requested = True
        return turn

    async def _begin(self, ticket: ActiveTurn) -> None:
        raise NotImplementedError

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        await self.close(reason="context exit")
        return False

    # meiva:note
    # An input delivered to an idle session starts a turn of ours: the turn record is created, what nobody
    # consumed while the session was idle is dropped (a turn that began from a queue the idle period filled
    # would otherwise read an event that belonged to no turn as its own, #5663), and the adapter's `_begin`
    # addresses the prompt to the CLI. This is the only place a turn of ours starts.
    # meiva:link base/lm/harness/docs/harness.md
    def _start_input_turn(self, entry: HarnessInput) -> ActiveTurn:
        turn = ActiveTurn(
            turn_id=self._mint_turn_id(),
            text=entry.text.strip(),
            key=entry.key,
            images=entry.images,
        )
        self._discard_unconsumed_events()
        self._active = turn
        _ = self._spawn(self._begin(turn))
        return turn

    # meiva:note
    # The one place a session decides which of its turns an incoming provider
    # message belongs to, so no adapter has to invent an owner. `origin_id` is the
    # provider's own name for the turn the message reports on, or None when the
    # provider does not name turns. Only a message that names the running turn's
    # own name — or names nothing at all — can be attributed to it: a name from a
    # turn this session never started, or already finished, has no owner, and the
    # caller routes it to `_unsolicited` instead. That is what keeps a provider's
    # own turn from being read as our turn's boundary (#5663).
    # meiva:link base/lm/harness/docs/harness.md
    def _claim(self, origin_id: str | None = None) -> ActiveTurn | None:
        ticket = self._active
        if ticket is None or (origin_id is not None and origin_id != ticket.provider_turn_id):
            return None
        return ticket

    # meiva:note
    # Output that belongs to no turn is inbound content, not a failure and not a
    # boundary: it is recorded in the event stream and handed to the sink, which
    # delivers it the way any other inbound reaches the agent. A session without a
    # sink (a demo, a unit test) still records it.
    # meiva:link base/lm/harness/docs/harness.md
    def _unsolicited(self, event: UnsolicitedOutput) -> None:
        self._emit(event)
        sink = self._unsolicited_sink
        if sink is not None:
            sink(event)

    @property
    def active_turn_id(self) -> str | None:
        return self._active.turn_id if self._active is not None else None

    @property
    def active_provider_turn_id(self) -> str | None:
        return self._active.provider_turn_id if self._active is not None else None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def record(self) -> SessionRecord | None:
        return self._record

    @property
    def resumed(self) -> bool:
        return self._resumed_existing

    @property
    def totals(self) -> SessionTotals:
        return self._totals

    @property
    def dropped_deltas(self) -> int:
        return self._dropped_deltas

    @property
    def dropped_events(self) -> int:
        return self._dropped_deltas

    # meiva:note
    # Events dropped because no turn was running when they arrived: the counter a
    # session's owner reads to see whether an idle provider said something nobody took.
    # meiva:link base/lm/harness/docs/attribution.md
    @property
    def unconsumed_events(self) -> int:
        return self._unconsumed_events

    async def events(self) -> AsyncIterator[BridgeEvent]:
        while True:
            event = await self._events.get()
            if event is EVENT_STREAM_END or not isinstance(event, BridgeEvent):
                return
            yield event

    def _now_iso(self) -> str:
        now = self._clock.now()
        return now.isoformat(timespec="seconds")

    def _monotonic(self) -> float:
        return self._clock.monotonic()

    def _resolve_resume(self, resume: str | None) -> str | None:
        resolved = resume or self._resume_from_registry()
        if resolved:
            self._resumed_existing = True
        return resolved

    # meiva:note
    # A fork's first start. When this agent was born as a fork, has no session of its own on this
    # harness, and the caller named no session to resume, the session to clone is the source agent's
    # registered one on this harness and machine. The host passes a source only while it runs on the
    # child's harness, so an older record the source kept on another harness is never cloned. A
    # source with no record here (it never started a session, or it runs on another machine) is a
    # refusal, never a quiet fresh session: a fork that silently lost its inherited conversation is
    # the wrong agent. Cloning counts as resuming, so the handoff document is not co-delivered: the
    # inherited conversation already holds it.
    # meiva:link base/lm/harness/docs/fork.md
    def _fork_origin(self, resolved: str | None) -> SessionRecord | None:
        if resolved or self._fork_source_agent_id is None or self._registry is None:
            return None
        source = self._registry.load(self._fork_source_agent_id, harness=self.harness)
        if source is None:
            raise ForkSourceUnavailableError(
                f"agent {self.agent_id} is a fork of agent {self._fork_source_agent_id}, but "
                f"this machine holds no CLI session for that agent to clone (it never started "
                f"one, or it runs on another machine); a fork cannot start without its source's "
                f"conversation"
            )
        self._resumed_existing = True
        return source

    def _resume_from_registry(self) -> str | None:
        if self._registry is None or self.agent_id is None:
            return None
        record = self._registry.load(self.agent_id, harness=self.harness)
        if record is None:
            return None
        self._resumed_existing = True
        self._record = record
        return record.session_id

    def _bind(self) -> None:
        if self._registry is None or self.agent_id is None or not self._session_id:
            return
        previous = self._registry.load(self.agent_id, harness=self.harness)
        now = self._now_iso()
        self._record = SessionRecord(
            agent_id=self.agent_id,
            session_id=self._session_id,
            cwd=str(self.config.cwd),
            model=self._model or self.config.model or "",
            created_at=previous.created_at if previous is not None else now,
            updated_at=now,
            resumes=(previous.resumes if previous is not None else 0)
            + (1 if self._resumed_existing else 0),
            harness=self.harness,
        )
        self._registry.save(self._record)
        self._emit(
            SessionBound(
                agent_id=self.agent_id, session_id=self._session_id, resumed=self._resumed_existing
            )
        )

    def _touch(self) -> None:
        if self._registry is None or self._record is None:
            return
        self._record = replace(self._record, updated_at=self._now_iso())
        self._registry.save(self._record)

    def _complete(self, turn: ActiveTurn) -> None:
        turn.interrupt_ready.set()
        if self._active is turn:
            self._active = None
        self._touch()

    # meiva:note
    # Idle means nothing of ours or the harness's is running: no ticket, no turn the harness began on
    # its own (`_observed`), and no hold. A turn we start while the harness runs one of its own would
    # land in the middle of it, so the adapter hands it to the running turn or refuses it.
    def _idle(self) -> bool:
        return self._active is None and not self._observed and not self._dispatch_held

    # meiva:note
    # A provider can run a turn of its own — the CLI answers a background-task
    # notification it injected without us — and while it does, its messages belong to
    # that turn. Holding dispatch keeps `_active` empty for its duration, so its text
    # takes the same path as any other idle message instead of being collected into a
    # turn of ours and committed as our reply. The bound exists because a provider
    # that never reports the end of its turn must cost one wait, not a wedged agent.
    # meiva:link base/lm/harness/docs/harness.md
    def _hold_dispatch(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError(f"a dispatch hold must expire, got {seconds}s")
        self._dispatch_held = True
        if self._release_timer is not None:
            self._release_timer.cancel()
        self._release_timer = self._spawn(self._release_after(seconds))

    async def _release_after(self, seconds: float) -> None:
        await self._clock.asleep(seconds)
        self._release_dispatch()

    def _release_dispatch(self) -> None:
        if not self._dispatch_held:
            return
        self._dispatch_held = False
        timer, self._release_timer = self._release_timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()

    # meiva:note
    # A turn sees only what happened during it. Whatever nobody consumed while the
    # session was idle is dropped here, at the one instant that is atomic with the
    # ticket becoming active. Left in the queue, an event that belonged to no turn —
    # a provider's report about its own turn, a leftover completion — is read by the
    # next turn as its own outcome, which is how an idle session poisoned a turn it
    # had nothing to do with (#5663). The stream's own end marker is not a leftover.
    # meiva:link base/lm/harness/docs/harness.md
    def _discard_unconsumed_events(self) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not isinstance(event, BridgeEvent):
                self._events.put_nowait(event)
                return
            self._unconsumed_events += 1

    def _fail_turn(self, turn: ActiveTurn, exc: BaseException) -> None:
        turn.interrupt_ready.set()
        self._emit(TurnFaulted(turn_id=turn.turn_id, error=f"turn failed: {exc}"))
        if self._active is turn:
            self._active = None

    def _fail_active(self, exc: BaseException) -> None:
        turn = self._active
        if turn is not None:
            self._fail_turn(turn, exc)

    def _emit(self, event: BridgeEvent) -> None:
        if is_droppable(event) and self._events.qsize() >= self.config.max_buffered_events:
            self._dropped_deltas += 1
            return
        self._events.put_nowait(event)

    async def _enter_tasks(self) -> None:
        if not self._tasks_entered:
            await self._tasks.__aenter__()
            self._tasks_entered = True

    def _spawn(self, coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
        task = self._tasks.create_task(coro)
        self._children.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._children.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._emit(SessionFailed(error=f"background task failed: {error}"))

    async def _cancel_children(self) -> None:
        children = [task for task in self._children if not task.done()]
        for task in children:
            task.cancel()
        if children:
            await asyncio.gather(*children, return_exceptions=True)

    async def _close_tasks(self) -> None:
        self._reader = None
        await self._cancel_children()
        if self._tasks_entered:
            self._tasks_entered = False
            try:
                await self._tasks.__aexit__(None, None, None)
            except ExceptionGroup as group:
                self._emit(SessionFailed(error=f"session tasks failed: {group!r}"))

    def _finish_close(self, reason: str) -> None:
        self._emit(SessionClosed(turn_id=None, reason=reason))
        self._events.put_nowait(EVENT_STREAM_END)
