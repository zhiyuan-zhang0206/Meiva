from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

from base.lm.harness.events import (
    BridgeEvent,
    HandoffInjected,
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
from base.lm.harness.turn import SessionTotals, TurnOutcome

# meiva:note
# Shared turn-engine state machine for every external harness session adapter:
# ordered inbound turn promotion, key-based idempotency replay, bounded outbound
# delta dropping, per-turn timeout watchdogs, background task-group lifecycle,
# and `SessionRegistry` binding/resumption.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
EVENT_STREAM_END = object()


class InboundQueueFullError(RuntimeError):
    pass


class SessionClosedError(RuntimeError):
    pass


class SessionClock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def asleep(self, seconds: float) -> Coroutine[object, object, None]: ...


class SessionConfig(Protocol):
    @property
    def cwd(self) -> Path: ...

    @property
    def model(self) -> str | None: ...

    @property
    def turn_timeout_seconds(self) -> float: ...

    @property
    def cancel_settle_seconds(self) -> float: ...

    @property
    def max_pending_turns(self) -> int: ...

    @property
    def max_buffered_events(self) -> int: ...

    @property
    def dedupe_window(self) -> int: ...


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
    ) -> None:
        self.turn_id = turn_id
        self.text = text
        self.key = key
        self.deduplicated = deduplicated
        self.submitted_at = submitted_at
        self.started_at: float | None = None
        # meiva:note
        # The name the provider gave the turn this ticket runs, when it gives one.
        # An adapter sets it as soon as it learns the name; `_claim` is the only
        # reader, and a ticket whose provider names no turns keeps it None.
        # meiva:link base/lm/harness/docs/harness.meiva.okf.md
        self.provider_turn_id: str | None = None
        self.interrupt_requested = False
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
        if not self._future.done():
            self._future.set_result(outcome)

    def _settle(self, outcome: TurnOutcome) -> None:
        self.settle(outcome)

    def fail(self, exc: BaseException) -> None:
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
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
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
        self._deadlines: dict[str, asyncio.Task[None]] = {}
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

    async def start(self, *, resume: str | None = None, handoff: Handoff | None = None) -> None:
        raise NotImplementedError

    async def close(self, *, reason: str = "closed") -> None:
        raise NotImplementedError

    async def interrupt(self) -> object:
        raise NotImplementedError

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

    def submit(self, text: str, *, key: str | None = None) -> TurnTicket:
        if self._closed:
            raise SessionClosedError("session is closed")
        trimmed = text.strip()
        if not trimmed:
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
        self._turn_seq += 1
        ticket = TurnTicket(
            turn_id=f"turn-{self._turn_seq}",
            text=trimmed,
            key=key,
            future=asyncio.get_running_loop().create_future(),
            submitted_at=self._monotonic(),
        )
        if self._active is None and not self._dispatch_held:
            self._start_turn(ticket)
        else:
            self._pending.append(ticket)
        return ticket

    # meiva:note
    # Every turn starts here, whether it was submitted into an idle session or promoted
    # from the queue after a hold, and every start discards what nobody consumed. A turn
    # that begins from a queue the idle period filled would otherwise read an event that
    # belonged to no turn as its own outcome (#5663).
    # meiva:link base/lm/harness/docs/harness.meiva.okf.md
    def _start_turn(self, ticket: TurnTicket) -> None:
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
    # meiva:link base/lm/harness/docs/harness.meiva.okf.md
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
    # meiva:link base/lm/harness/docs/harness.meiva.okf.md
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
    # meiva:link base/lm/harness/docs/attribution.meiva.okf.md
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

    def _start_deadline(self, ticket: TurnTicket) -> None:
        async def expire() -> None:
            await self._clock.asleep(self.config.turn_timeout_seconds)
            if self._active is not ticket:
                return
            self._emit(
                TurnFaulted(
                    turn_id=ticket.turn_id,
                    error=f"turn exceeded {self.config.turn_timeout_seconds:.0f}s; interrupting",
                )
            )
            try:
                await self.interrupt()
            except Exception as exc:
                self._emit(TurnFaulted(turn_id=ticket.turn_id, error=f"interrupt failed: {exc}"))

        self._deadlines[ticket.turn_id] = self._spawn(expire())

    def _cancel_deadline(self, ticket: TurnTicket) -> None:
        task = self._deadlines.pop(ticket.turn_id, None)
        if task is not None:
            task.cancel()

    def _complete(self, ticket: TurnTicket, outcome: TurnOutcome) -> None:
        self._cancel_deadline(ticket)
        if self._active is ticket:
            self._active = None
        if ticket.key is not None:
            self._remember(ticket.key, outcome)
        self._touch()
        ticket.settle(outcome)
        self._promote()

    def _promote(self) -> None:
        if self._closed or self._dispatch_held or self._active is not None or not self._pending:
            return
        self._start_turn(self._pending.popleft())

    # meiva:note
    # A provider can run a turn of its own — the CLI answers a background-task
    # notification it injected without us — and while it does, its messages belong to
    # that turn. Holding dispatch keeps `_active` empty for its duration, so its text
    # takes the same path as any other idle message instead of being collected into a
    # turn of ours and committed as our reply. The bound exists because a provider
    # that never reports the end of its turn must cost one wait, not a wedged agent.
    # meiva:link base/lm/harness/docs/harness.meiva.okf.md
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
    # meiva:link base/lm/harness/docs/harness.meiva.okf.md
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
        self._cancel_deadline(ticket)
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
        for task in list(self._deadlines.values()):
            task.cancel()
        self._deadlines.clear()
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
