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
    SessionFaulted,
    Usage,
    is_droppable,
)
from base.lm.harness.handoff import Handoff
from base.lm.harness.process import ChildProbe, ExitStatus, PsutilChildProbe
from base.lm.harness.registry import SessionRecord, SessionRegistry

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


def add_cost(left: float | None, right: float | None) -> float | None:
    if left is None and right is None:
        return None
    return (left or 0.0) + (right or 0.0)


_add_cost = add_cost


@dataclass(frozen=True, slots=True)
class SessionTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float | None = 0.0
    turns: int = 0
    per_turn_usage: bool = True
    cost_known: bool = True

    def with_turn(
        self,
        usage: Usage,
        cost_usd: float | None = None,
        *,
        measured: bool = True,
        cost: float | None = None,
    ) -> SessionTotals:
        turn_cost = cost if cost is not None else cost_usd
        return SessionTotals(
            input_tokens=self.input_tokens + usage.input_tokens,
            output_tokens=self.output_tokens + usage.output_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + usage.cache_creation_input_tokens
            ),
            cache_read_input_tokens=(self.cache_read_input_tokens + usage.cache_read_input_tokens),
            cost_usd=_add_cost(self.cost_usd, turn_cost),
            turns=self.turns + 1,
            per_turn_usage=self.per_turn_usage and measured,
            cost_known=self.cost_known and turn_cost is not None,
        )


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    turn_id: str
    text: str
    subtype: str
    is_error: bool
    interrupted: bool
    stop_reason: str | None
    usage: Usage
    cost_usd: float | None
    permission_denials: tuple[str, ...]
    num_turns: int = 1
    steps: tuple[Usage, ...] = ()
    per_turn_usage: bool = True


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
# Where a harness session is recorded: the agent it serves and the registry that maps that agent
# to its external session id. Both are absent for a session no agent owns (a demo, a unit test).
@dataclass(frozen=True)
class SessionBinding:
    agent_id: int | None = None
    registry: SessionRegistry | None = None


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
        if self._active is None:
            self._active = ticket
            self._spawn(self._begin(ticket))
        else:
            self._pending.append(ticket)
        return ticket

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
                SessionFaulted(
                    turn_id=ticket.turn_id,
                    error=f"turn exceeded {self.config.turn_timeout_seconds:.0f}s; interrupting",
                )
            )
            try:
                await self.interrupt()
            except Exception as exc:
                self._emit(SessionFaulted(turn_id=ticket.turn_id, error=f"interrupt failed: {exc}"))

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
        if self._closed or self._active is not None or not self._pending:
            return
        ticket = self._pending.popleft()
        self._active = ticket
        self._spawn(self._begin(ticket))

    def _fail_turn(self, ticket: TurnTicket, exc: BaseException) -> None:
        self._cancel_deadline(ticket)
        self._emit(SessionFaulted(turn_id=ticket.turn_id, error=f"turn failed: {exc}"))
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
            self._emit(SessionFaulted(turn_id=None, error=f"background task failed: {error}"))

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
                self._emit(SessionFaulted(turn_id=None, error=f"session tasks failed: {group!r}"))

    def _finish_close(self, reason: str) -> None:
        self._emit(SessionClosed(turn_id=None, reason=reason))
        self._events.put_nowait(EVENT_STREAM_END)
