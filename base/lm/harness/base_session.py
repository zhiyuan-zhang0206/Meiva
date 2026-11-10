from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Coroutine, Sequence
from dataclasses import dataclass, replace
from types import TracebackType
from typing import Self

from base.lm.harness.delivery import DeliveryLedger, HarnessInput, ImageBlock, LedgerStep
from base.lm.harness.events import (
    BridgeEvent,
    HandoffInjected,
    InputInContext,
    InputLost,
    InputRejected,
    SessionBound,
    SessionClosed,
    SessionFailed,
    TurnFaulted,
    UnsolicitedOutput,
    UnsolicitedSink,
    is_droppable,
)
from base.lm.harness.handoff import Handoff
from base.lm.harness.process import ChildProbe, ExitStatus, PsutilChildProbe
from base.lm.harness.registry import SessionRecord, SessionRegistry
from base.lm.harness.session import (
    EVENT_STREAM_END,
    InboundQueueFullError,
    SessionClock,
    SessionClosedError,
    SessionConfig,
    SessionTotals,
    TurnOutcome,
)

# meiva:note
# Shared turn-engine state machine for every external harness session adapter:
# ordered inbound turn promotion, key-based idempotency replay, bounded outbound
# delta dropping, background task-group lifecycle,
# and `SessionRegistry` binding/resumption.
# meiva:link base/lm/harness/docs/harness.md


class TurnTicket:
    def __init__(
        self,
        *,
        turn_id: str,
        text: str,
        key: str | None,
        future: asyncio.Future[TurnOutcome],
        deduplicated: bool = False,
        submitted_at: float,
        input_key: str | None = None,
        images: tuple[ImageBlock, ...] = (),
    ) -> None:
        self.turn_id = turn_id
        self.text = text
        self.key = key
        # meiva:note
        # The id the delivery events name this ticket's input by. `key` is the idempotency key a
        # caller replays an outcome with; a ticket that starts a turn for a delivered input keeps
        # `key` empty (a re-delivery must reach the harness, not replay an old outcome) and carries
        # the input's own key here. A ticket without either falls back to its turn id.
        # meiva:link base/lm/harness/docs/delivery.md
        self.input_key = input_key or key or turn_id
        self.images = images
        self.deduplicated = deduplicated
        self.submitted_at = submitted_at
        self.started_at: float | None = None
        # meiva:note
        # The name the provider gave the turn this ticket runs, when it gives one.
        # An adapter sets it as soon as it learns the name; `_claim` is the only
        # reader, and a ticket whose provider names no turns keeps it None.
        # meiva:link base/lm/harness/docs/harness.md
        self.provider_turn_id: str | None = None
        self.interrupt_requested = False
        self.interrupt_ready = asyncio.Event()
        self.parts: list[str] = []
        self.thoughts: list[str] = []
        self.denials: list[str] = []
        self._future = future

    async def outcome(self) -> TurnOutcome:
        return await self._future

    @property
    def settled(self) -> bool:
        return self._future.done()

    def settle(self, outcome: TurnOutcome) -> None:
        self.interrupt_ready.set()
        if not self._future.done():
            self._future.set_result(outcome)

    def _settle(self, outcome: TurnOutcome) -> None:
        self.settle(outcome)

    def fail(self, exc: BaseException) -> None:
        self.interrupt_ready.set()
        if self._future.done():
            return
        self._future.set_exception(exc)
        self._future.exception()

    def _fail(self, exc: BaseException) -> None:
        self.fail(exc)


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


class BaseHarnessSession[ConfigT: SessionConfig](ExitStatus):
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
        self._pending: deque[TurnTicket] = deque()
        self._completed: OrderedDict[str, TurnOutcome] = OrderedDict()
        self._active: TurnTicket | None = None
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
        self._handoff_ticket: TurnTicket | None = None
        self._turn_seq = 0
        self._unsolicited_sink = binding.unsolicited
        self._dispatch_held = False
        self._release_timer: asyncio.Task[None] | None = None
        self._unconsumed_events = 0
        self._ledger = DeliveryLedger()
        self._observed: set[str] = set()
        self._waiters: dict[str, asyncio.Future[BridgeEvent]] = {}

    async def start(self, *, resume: str | None = None, handoff: Handoff | None = None) -> None:
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
    def steering_available(self) -> bool:
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
            self._settle_waiter(event)

    def _settle_waiter(self, event: BridgeEvent) -> None:
        if isinstance(event, (InputInContext, InputRejected, InputLost)):
            waiter = self._waiters.get(event.key)
            if waiter is not None and not waiter.done():
                waiter.set_result(event)

    def _fail_waiters(self, exc: BaseException) -> None:
        for waiter in self._waiters.values():
            if not waiter.done():
                waiter.set_exception(exc)
                _ = waiter.exception()

    # meiva:note
    # The steering operation the graph still uses, kept as a thin wrapper over `deliver`. Its
    # answer is now a fact, not a write result: True only when the input entered the context of the
    # turn that was running when it was handed over, False when the harness refused it (so it can go
    # back to the queue), and an error when the input is lost, or became part of another turn, because
    # then nothing can say it is safe to hand over again.
    # meiva:link base/lm/harness/docs/delivery.md
    async def steer(self, text: str, *, key: str) -> bool:
        if not text.strip() or not key:
            raise ValueError("steering requires nonempty text and a message key")
        if not self.steering_available:
            return False
        ticket = self._active
        if ticket is None:
            return False
        if key in self._waiters:
            raise RuntimeError("steer request already pending for this message key")
        waiter: asyncio.Future[BridgeEvent] = asyncio.get_running_loop().create_future()
        self._waiters[key] = waiter
        try:
            await self.deliver([HarnessInput(key=key, text=text)])
            settled = await waiter
        finally:
            del self._waiters[key]
        match settled:
            case InputRejected():
                return False
            case InputInContext(turn_id=entered) if entered == ticket.turn_id:
                return True
            case InputInContext(turn_id=entered):
                raise RuntimeError(f"steered input entered turn {entered}, not the running turn")
            case _:
                raise RuntimeError("turn ended before the steer receipt arrived")

    # meiva:note
    # Startup and completion can both race a cancel. Wait on the captured ticket
    # for the adapter to finish addressing the prompt, then recheck ownership.
    async def _interrupt_target(self, turn_id: str | None) -> TurnTicket | None:
        ticket = self._active
        if turn_id is not None and (ticket is None or ticket.turn_id != turn_id):
            ticket = next((queued for queued in self._pending if queued.turn_id == turn_id), None)
        if ticket is None:
            return None
        await ticket.interrupt_ready.wait()
        if self._active is not ticket or ticket.interrupt_requested:
            return None
        ticket.interrupt_requested = True
        return ticket

    async def _begin(self, ticket: TurnTicket) -> None:
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

    def submit(
        self,
        text: str,
        *,
        key: str | None = None,
        input_key: str | None = None,
        images: tuple[ImageBlock, ...] = (),
    ) -> TurnTicket:
        if self._closed:
            raise SessionClosedError("session is closed")
        trimmed = text.strip()
        if not trimmed and not images:
            raise ValueError("inbound message is empty")
        if key is not None:
            existing = self._recall(key)
            if existing is not None:
                return existing
        if len(self._pending) >= self.config.max_pending_turns:
            raise InboundQueueFullError(
                f"{len(self._pending)} turns already queued "
                f"(limit {self.config.max_pending_turns}); keep the message and retry"
            )
        ticket = TurnTicket(
            turn_id=self._mint_turn_id(),
            text=trimmed,
            key=key,
            future=asyncio.get_running_loop().create_future(),
            submitted_at=self._monotonic(),
            input_key=input_key,
            images=images,
        )
        if self._idle():
            self._start_turn(ticket)
        else:
            self._pending.append(ticket)
        return ticket

    # meiva:note
    # Every turn starts here, whether it was submitted into an idle session or promoted
    # from the queue after a hold, and every start discards what nobody consumed. A turn
    # that begins from a queue the idle period filled would otherwise read an event that
    # belonged to no turn as its own outcome (#5663).
    # meiva:link base/lm/harness/docs/harness.md
    def _start_turn(self, ticket: TurnTicket, *, keep_events: bool = False) -> None:
        if not keep_events:
            self._discard_unconsumed_events()
        self._active = ticket
        self._spawn(self._begin(ticket))

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
    def _claim(self, origin_id: str | None = None) -> TurnTicket | None:
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
    def pending_turns(self) -> tuple[str, ...]:
        return tuple(ticket.turn_id for ticket in self._pending)

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def handoff_ticket(self) -> TurnTicket | None:
        return self._handoff_ticket

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

    def _resume_from_registry(self) -> str | None:
        if self._registry is None or self.agent_id is None:
            return None
        record = self._registry.load(self.agent_id)
        if record is None:
            return None
        self._resumed_existing = True
        self._record = record
        return record.session_id

    def _inject_handoff(self, handoff: Handoff | None) -> None:
        if handoff is None or self._resumed_existing:
            return
        key = f"handoff:{self.agent_id}" if self.agent_id is not None else None
        ticket = self.submit(handoff.render(), key=key)
        self._handoff_ticket = ticket
        self._emit(
            HandoffInjected(
                agent_id=self.agent_id or 0,
                turn_id=ticket.turn_id,
                size_bytes=handoff.size_bytes(),
            )
        )

    def _bind(self) -> None:
        if self._registry is None or self.agent_id is None or not self._session_id:
            return
        previous = self._registry.load(self.agent_id)
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

    def _complete(self, ticket: TurnTicket, outcome: TurnOutcome) -> None:
        if self._active is ticket:
            self._active = None
        if ticket.key is not None:
            self._remember(ticket.key, outcome)
        self._touch()
        ticket.settle(outcome)
        self._promote()

    # meiva:note
    # Idle means nothing of ours or the harness's is running: no ticket, no turn the harness began on
    # its own (`_observed`), and no hold. A turn we start while the harness runs one of its own would
    # land in the middle of it, so it waits in the queue.
    def _idle(self) -> bool:
        return self._active is None and not self._observed and not self._dispatch_held

    # meiva:note
    # `keep_events` is for the moment a turn the harness ran on its own has just ended: its events
    # are the turn's own, not leftovers of an idle period, and the consumer may not have read them yet.
    def _promote(self, *, keep_events: bool = False) -> None:
        if self._closed or not self._idle() or not self._pending:
            return
        self._start_turn(self._pending.popleft(), keep_events=keep_events)

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
        self._promote()

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

    def _fail_turn(self, ticket: TurnTicket, exc: BaseException) -> None:
        self._emit(TurnFaulted(turn_id=ticket.turn_id, error=f"turn failed: {exc}"))
        ticket.fail(SessionClosedError(f"turn failed: {exc}"))
        if self._active is ticket:
            self._active = None
        self._promote()

    def _fail_active(self, exc: BaseException) -> None:
        ticket = self._active
        if ticket is not None:
            self._fail_turn(ticket, exc)
        self._fail_pending(SessionClosedError(f"session stream failed: {exc}"))

    def _fail_pending(self, exc: BaseException) -> None:
        self._fail_waiters(exc)
        queued = list(self._pending)
        self._pending.clear()
        for ticket in queued:
            ticket.fail(exc)
        active = self._active
        if active is not None:
            active.fail(exc)
            self._active = None

    def _recall(self, key: str) -> TurnTicket | None:
        active = self._active
        if active is not None and active.key == key:
            return active
        for ticket in self._pending:
            if ticket.key == key:
                return ticket
        completed = self._completed.get(key)
        if completed is None:
            return None
        future: asyncio.Future[TurnOutcome] = asyncio.get_running_loop().create_future()
        future.set_result(completed)
        return TurnTicket(
            turn_id=completed.turn_id,
            text=completed.text,
            key=key,
            future=future,
            deduplicated=True,
            submitted_at=self._monotonic(),
        )

    def _remember(self, key: str, outcome: TurnOutcome) -> None:
        self._completed[key] = outcome
        while len(self._completed) > self.config.dedupe_window:
            self._completed.popitem(last=False)

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
