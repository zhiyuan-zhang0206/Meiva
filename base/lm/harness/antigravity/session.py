from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.mapping import (
    assistant_text,
    map_init,
    map_step_update,
    parse_result,
)
from base.lm.harness.antigravity.transport import (
    AgyTransport,
    InterruptReport,
    TransportClosedError,
)
from base.lm.harness.events import (
    BridgeEvent,
    HandoffInjected,
    SessionBound,
    SessionClosed,
    SessionFaulted,
    TurnCompleted,
    Usage,
    is_droppable,
)
from base.lm.harness.handoff import Handoff
from base.lm.harness.process import ChildProbe, PsutilChildProbe, snapshot
from base.lm.harness.registry import SessionRecord, SessionRegistry

# meiva:note
# Same three boundaries as the Claude bridge — ordered inbound, one outbound
# stream, an assertable cancel — with one harness difference that shapes the
# whole file: an interrupt kills the `agy` process. `interrupt()` therefore
# retires the transport, and the next turn respawns it with `--conversation`, so
# the transcript survives even though the process does not.
# meiva:link base/lm/harness/antigravity/docs/antigravity.meiva.okf.md
CancelReport = InterruptReport
EVENT_STREAM_END = object()


class InboundQueueFullError(RuntimeError):
    pass


class SessionClosedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SessionTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float | None = None
    turns: int = 0

    def with_turn(self, usage: Usage, cost_usd: float | None) -> SessionTotals:
        return SessionTotals(
            input_tokens=self.input_tokens + usage.input_tokens,
            output_tokens=self.output_tokens + usage.output_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + usage.cache_creation_input_tokens
            ),
            cache_read_input_tokens=self.cache_read_input_tokens + usage.cache_read_input_tokens,
            cost_usd=_add_cost(self.cost_usd, cost_usd),
            turns=self.turns + 1,
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


class TurnTicket:
    def __init__(
        self,
        *,
        turn_id: str,
        text: str,
        key: str | None,
        future: asyncio.Future[TurnOutcome],
        deduplicated: bool = False,
    ) -> None:
        self.turn_id = turn_id
        self.text = text
        self.key = key
        self.deduplicated = deduplicated
        self.submitted_at = time.monotonic()
        self.started_at: float | None = None
        self.interrupt_requested = False
        self.parts: list[str] = []
        self._future = future

    async def outcome(self) -> TurnOutcome:
        return await self._future

    @property
    def settled(self) -> bool:
        return self._future.done()

    def _settle(self, outcome: TurnOutcome) -> None:
        if not self._future.done():
            self._future.set_result(outcome)

    def _fail(self, exc: BaseException) -> None:
        if self._future.done():
            return
        self._future.set_exception(exc)
        self._future.exception()


class AgySession:
    """One `agy` conversation as a turn engine."""

    def __init__(
        self,
        config: AntigravityConfig,
        *,
        environ: Mapping[str, str] | None = None,
        agent_id: int | None = None,
        registry: SessionRegistry | None = None,
        probe: ChildProbe | None = None,
        transport_factory: Any | None = None,
    ) -> None:
        self.config = config
        self.agent_id = agent_id
        self._probe: ChildProbe = probe or PsutilChildProbe()
        self._registry = registry
        self._environ = dict(environ) if environ else None
        self._factory = transport_factory or self._default_transport
        self._events: asyncio.Queue[Any] = asyncio.Queue()
        self._pending: deque[TurnTicket] = deque()
        self._completed: OrderedDict[str, TurnOutcome] = OrderedDict()
        self._active: TurnTicket | None = None
        self._transport: AgyTransport | None = None
        self._retired: AgyTransport | None = None
        self._reader: asyncio.Task[None] | None = None
        self._deadlines: dict[str, asyncio.Task[None]] = {}
        self._tasks = asyncio.TaskGroup()
        self._children: set[asyncio.Task[Any]] = set()
        self._tasks_entered = False
        self._totals = SessionTotals()
        self._session_id: str | None = None
        self._baseline: dict[int, str] = {}
        self._closed = False
        self._dropped_deltas = 0
        self._resumed_existing = False
        self._record: SessionRecord | None = None
        self._handoff_ticket: TurnTicket | None = None
        self._step_text: dict[int, list[str]] = {}

    async def start(self, *, resume: str | None = None, handoff: Handoff | None = None) -> None:
        if self._transport is not None:
            raise RuntimeError("session already started")
        if self._closed:
            raise SessionClosedError("session is closed")
        resume = resume or self._resume_from_registry()
        if resume is not None:
            # A caller-supplied resume is still a resume: the transcript already
            # carries the handoff, so it must not be injected again.
            self._resumed_existing = True
        transport = self._factory()
        await transport.start(resume=resume)
        self._transport = transport
        if not self._tasks_entered:
            await self._tasks.__aenter__()
            self._tasks_entered = True
        self._reader = self._spawn(self._read_loop(transport))
        self._inject_handoff(handoff)

    async def close(self, *, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        self._fail_pending(SessionClosedError("session closed"))
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
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()
        self._emit(SessionClosed(turn_id=None, reason=reason))
        self._events.put_nowait(EVENT_STREAM_END)

    async def __aenter__(self) -> AgySession:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
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
        ticket = TurnTicket(
            turn_id=f"turn-{uuid4().hex[:12]}",
            text=trimmed,
            key=key,
            future=asyncio.get_running_loop().create_future(),
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
    def cli_pid(self) -> int | None:
        return self._transport.pid if self._transport is not None else None

    @property
    def totals(self) -> SessionTotals:
        return self._totals

    @property
    def dropped_deltas(self) -> int:
        return self._dropped_deltas

    async def interrupt(self) -> CancelReport:
        ticket = self._active
        transport = self._transport
        if ticket is not None:
            ticket.interrupt_requested = True
        if transport is None:
            return CancelReport(
                turn_id=ticket.turn_id if ticket is not None else None,
                interrupt_ms=0.0,
                settled=None,
                observed=(),
                remaining=(),
                waited_ms=0.0,
                cli_pid=None,
                cli_alive=None,
            )
        if ticket is None:
            # Nothing is running: keep the healthy process instead of killing it.
            return CancelReport(
                turn_id=None,
                interrupt_ms=0.0,
                settled=None,
                observed=(),
                remaining=(),
                waited_ms=0.0,
                cli_pid=transport.pid,
                cli_alive=transport.alive,
            )
        # Retire before signalling: the reader may see EOF while the report is
        # still being assembled, and an expected exit must not look like a crash.
        self._retired = transport
        return await transport.interrupt(turn_id=ticket.turn_id, baseline=self._baseline)

    async def events(self) -> AsyncIterator[BridgeEvent]:
        while True:
            event = await self._events.get()
            if event is EVENT_STREAM_END:
                return
            yield event

    def _default_transport(self) -> AgyTransport:
        return AgyTransport(self.config, environ=self._environ, probe=self._probe)

    async def _ensure_transport(self) -> AgyTransport:
        if self._closed:
            raise SessionClosedError("session is closed")
        current = self._transport
        if current is not None and current is not self._retired and current.alive:
            return current
        if current is not None:
            self._transport = None
            await current.close()
        transport = self._factory()
        await transport.start(resume=self._session_id)
        self._transport = transport
        self._retired = None
        self._reader = self._spawn(self._read_loop(transport))
        return transport

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
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self._record = SessionRecord(
            agent_id=self.agent_id,
            session_id=self._session_id,
            cwd=str(self.config.cwd),
            model=self.config.model,
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
        self._record = SessionRecord(
            agent_id=self._record.agent_id,
            session_id=self._record.session_id,
            cwd=self._record.cwd,
            model=self._record.model,
            created_at=self._record.created_at,
            updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            resumes=self._record.resumes,
        )
        self._registry.save(self._record)

    async def _begin(self, ticket: TurnTicket) -> None:
        ticket.started_at = time.monotonic()
        self._step_text = {}
        try:
            transport = await self._ensure_transport()
            self._baseline = snapshot(self._probe, transport.pid)
            self._start_deadline(ticket)
            await transport.send(ticket.text)
        except (TransportClosedError, OSError, RuntimeError) as exc:
            self._fail_turn(ticket, exc)

    def _start_deadline(self, ticket: TurnTicket) -> None:
        async def expire() -> None:
            await asyncio.sleep(self.config.turn_timeout_seconds)
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

    async def _read_loop(self, transport: AgyTransport) -> None:
        try:
            async for payload in transport.events():
                self._handle(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(SessionFaulted(turn_id=None, error=f"session stream failed: {exc}"))
            self._fail_active(exc)
        finally:
            await self._on_stream_end(transport)

    async def _on_stream_end(self, transport: AgyTransport) -> None:
        if transport is not self._transport:
            await transport.close()
            return
        self._transport = None
        retired = transport is self._retired
        if retired:
            self._retired = None
        await transport.close()
        if retired or self._closed:
            return
        ticket = self._active
        if ticket is not None and ticket.interrupt_requested:
            # The interrupt killed the process before its `result` arrived.
            self._complete(
                ticket,
                TurnOutcome(
                    turn_id=ticket.turn_id,
                    text="".join(ticket.parts).strip(),
                    subtype="interrupted",
                    is_error=True,
                    interrupted=True,
                    stop_reason="interrupted",
                    usage=Usage(),
                    cost_usd=None,
                    permission_denials=(),
                ),
            )
            return
        error = "agy stream ended"
        self._emit(
            SessionFaulted(turn_id=ticket.turn_id if ticket is not None else None, error=error)
        )
        self._fail_active(SessionClosedError(error))

    def _handle(self, payload: dict[str, Any]) -> None:
        name = payload.get("event")
        if name == "init":
            event = map_init(payload)
            self._session_id = event.session_id or self._session_id
            self._emit(event)
            self._bind()
            return
        ticket = self._active
        if ticket is None:
            return
        if name == "step_update":
            raw = payload.get("step_update")
            if not isinstance(raw, dict):
                return
            update = cast("dict[str, Any]", raw)
            self._accumulate(update)
            for event in map_step_update(update, turn_id=ticket.turn_id):
                self._emit(event)
            if update.get("state") == "DONE":
                done = assistant_text(
                    update, turn_id=ticket.turn_id, text=self._take_step_text(update)
                )
                if done is not None and done.text:
                    ticket.parts.append(done.text)
                    self._emit(done)
            return
        if name == "result":
            self._handle_result(payload.get("result"), ticket)

    def _accumulate(self, update: dict[str, Any]) -> None:
        if update.get("step_type") != "agent_response":
            return
        index = update.get("step_index", 0)
        delta = update.get("text_delta")
        bucket = self._step_text.setdefault(index if isinstance(index, int) else 0, [])
        if isinstance(delta, str) and delta:
            bucket.append(delta)

    def _take_step_text(self, update: dict[str, Any]) -> str:
        index = update.get("step_index", 0)
        bucket = self._step_text.pop(index if isinstance(index, int) else 0, [])
        return "".join(bucket).strip()

    def _handle_result(self, payload: Any, ticket: TurnTicket) -> None:
        fields = parse_result(cast("dict[str, Any]", payload) if isinstance(payload, dict) else {})
        text = fields.text.strip() or "".join(ticket.parts).strip()
        outcome = TurnOutcome(
            turn_id=ticket.turn_id,
            text=text,
            subtype=fields.subtype,
            is_error=fields.is_error,
            interrupted=ticket.interrupt_requested or fields.interrupted,
            stop_reason=fields.status,
            usage=fields.usage,
            cost_usd=None,
            permission_denials=fields.denied_actions,
        )
        self._totals = self._totals.with_turn(fields.usage, None)
        if fields.session_id:
            self._session_id = fields.session_id
        self._emit(
            TurnCompleted(
                turn_id=ticket.turn_id,
                session_id=fields.session_id or self._session_id,
                text=text,
                subtype=fields.subtype,
                is_error=fields.is_error,
                interrupted=ticket.interrupt_requested or fields.interrupted,
                stop_reason=fields.status,
                num_turns=fields.num_turns,
                duration_ms=fields.duration_ms,
                usage=fields.usage,
                cost_usd=None,
                cumulative_cost_usd=self._totals.cost_usd,
                permission_denials=fields.denied_actions,
            )
        )
        self._complete(ticket, outcome)

    def _complete(self, ticket: TurnTicket, outcome: TurnOutcome) -> None:
        self._cancel_deadline(ticket)
        ticket._settle(outcome)
        if ticket.key is not None:
            self._remember(ticket.key, outcome)
        self._touch()
        if self._active is ticket:
            self._active = None
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
        ticket._fail(SessionClosedError(f"turn failed: {exc}"))
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
            ticket._fail(exc)
        active = self._active
        if active is not None:
            active._fail(exc)
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
        )

    def _remember(self, key: str, outcome: TurnOutcome) -> None:
        self._completed[key] = outcome
        while len(self._completed) > self.config.dedupe_window:
            self._completed.popitem(last=False)

    def _cancel_deadline(self, ticket: TurnTicket) -> None:
        task = self._deadlines.pop(ticket.turn_id, None)
        if task is not None:
            task.cancel()

    def _emit(self, event: BridgeEvent) -> None:
        if is_droppable(event) and self._events.qsize() >= self.config.max_buffered_events:
            self._dropped_deltas += 1
            return
        self._events.put_nowait(event)

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = self._tasks.create_task(coro)
        self._children.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
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


def _add_cost(left: float | None, right: float | None) -> float | None:
    if left is None and right is None:
        return None
    return (left or 0.0) + (right or 0.0)
