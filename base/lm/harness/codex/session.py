from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from base.lm.harness.codex.config import CodexConfig
from base.lm.harness.codex.mapping import (
    approval_denial,
    assistant_text,
    is_final_answer,
    item_event,
    parse_turn,
    text_delta,
    usage_from,
)
from base.lm.harness.codex.transport import (
    AppServerTransport,
    JsonObject,
    JsonRpcError,
    StdioAppServer,
    TransportClosedError,
    as_object,
    as_text,
)
from base.lm.harness.events import (
    AssistantText,
    BridgeEvent,
    HandoffInjected,
    SessionBound,
    SessionClosed,
    SessionFaulted,
    SessionStarted,
    TurnCompleted,
    Usage,
    is_droppable,
)
from base.lm.harness.handoff import Handoff
from base.lm.harness.process import (
    ChildProbe,
    ProcessObservation,
    PsutilChildProbe,
    describe,
    snapshot,
    wait_gone,
)
from base.lm.harness.registry import SessionRecord, SessionRegistry

# meiva:note
# The three boundaries are the ones every harness bridge has, expressed in
# app-server terms. Inbound waits for `turn/completed` because `turn/start` on a
# busy thread steers the running turn, and a steered message lands inside a tool
# result rather than at a turn boundary. Outbound is one queue; text deltas are
# droppable, control events are not. Cancellation is `turn/interrupt` plus an
# assertion that the commands the turn spawned are gone, and `settled=None` when
# the app-server pid is unobservable — never a silent pass.
# meiva:link base/lm/harness/codex/docs/codex.meiva.okf.md
CANCEL_POLL_SECONDS = 0.02
_CLIENT_NAME = "meiva-codex-bridge"
_CLIENT_VERSION = "1"
_EVENT_STREAM_END = object()
_INTERRUPT_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "applyPatchApproval",
    }
)


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
    cost_usd: float = 0.0
    turns: int = 0

    def with_turn(self, usage: Usage, cost_usd: float | None) -> SessionTotals:
        return SessionTotals(
            input_tokens=self.input_tokens + usage.input_tokens,
            output_tokens=self.output_tokens + usage.output_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + usage.cache_creation_input_tokens
            ),
            cache_read_input_tokens=self.cache_read_input_tokens + usage.cache_read_input_tokens,
            cost_usd=self.cost_usd + (cost_usd or 0.0),
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
        self.denials: list[str] = []
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


class CodexSession:
    def __init__(
        self,
        config: CodexConfig,
        *,
        environ: Mapping[str, str] | None = None,
        agent_id: int | None = None,
        registry: SessionRegistry | None = None,
        transport: AppServerTransport | None = None,
        probe: ChildProbe | None = None,
    ) -> None:
        self.config = config
        self.agent_id = agent_id
        self._probe: ChildProbe = probe or PsutilChildProbe()
        self._registry = registry
        self._environ = dict(environ) if environ is not None else None
        self._transport: AppServerTransport = transport or StdioAppServer(
            config.command(),
            cwd=config.cwd,
            environ=self._environ,
            request_timeout=config.startup_timeout_seconds,
        )
        self._events: asyncio.Queue[Any] = asyncio.Queue()
        self._pending: deque[TurnTicket] = deque()
        self._completed: OrderedDict[str, TurnOutcome] = OrderedDict()
        self._active: TurnTicket | None = None
        self._deadlines: dict[str, asyncio.Task[None]] = {}
        self._tasks = asyncio.TaskGroup()
        self._children: set[asyncio.Task[Any]] = set()
        self._tasks_entered = False
        self._totals = SessionTotals()
        self._turn_ids: dict[str, str] = {}
        self._turn_usage: dict[str, Usage] = {}
        self._final_parts: dict[str, list[str]] = {}
        self._session_id: str | None = None
        self._baseline: dict[int, str] = {}
        self._closed = False
        self._started = False
        self._dropped_deltas = 0
        self._resumed_existing = False
        self._record: SessionRecord | None = None
        self._handoff_ticket: TurnTicket | None = None

    async def start(self, *, resume: str | None = None, handoff: Handoff | None = None) -> None:
        if self._started:
            raise RuntimeError("session already started")
        if self._closed:
            raise SessionClosedError("session is closed")
        self._transport.on_notification(self._on_notification)
        self._transport.on_request(self._on_request)
        self._transport.on_closed(self._on_transport_closed)
        await self._transport.start()
        if not self._tasks_entered:
            await self._tasks.__aenter__()
            self._tasks_entered = True
        self._started = True
        await self._initialize()
        await self._open_thread(resume or self._resume_from_registry())
        self._bind()
        self._inject_handoff(handoff)

    async def close(self, *, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        self._fail_pending(SessionClosedError("session closed"))
        for task in list(self._deadlines.values()):
            task.cancel()
        self._deadlines.clear()
        await self._cancel_children()
        if self._tasks_entered:
            self._tasks_entered = False
            try:
                await self._tasks.__aexit__(None, None, None)
            except ExceptionGroup as group:
                self._emit(SessionFaulted(turn_id=None, error=f"session tasks failed: {group!r}"))
        await self._transport.close()
        self._emit(SessionClosed(turn_id=None, reason=reason))
        self._events.put_nowait(_EVENT_STREAM_END)

    async def __aenter__(self) -> CodexSession:
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
        return self._transport.pid

    @property
    def totals(self) -> SessionTotals:
        return self._totals

    @property
    def dropped_deltas(self) -> int:
        return self._dropped_deltas

    async def interrupt(self) -> CancelReport:
        ticket = self._active
        if ticket is not None:
            ticket.interrupt_requested = True
        pid = self._transport.pid
        observed = snapshot(self._probe, pid)
        spawned = {
            child: command for child, command in observed.items() if child not in self._baseline
        }
        started = time.perf_counter()
        if ticket is not None and self._session_id:
            codex_turn_id = self._codex_turn_for(ticket.turn_id)
            try:
                await self._transport.request(
                    "turn/interrupt",
                    {"threadId": self._session_id, "turnId": codex_turn_id},
                    timeout=self.config.startup_timeout_seconds,
                )
            except (JsonRpcError, TransportClosedError, TimeoutError) as exc:
                self._emit(
                    SessionFaulted(turn_id=ticket.turn_id, error=f"turn/interrupt failed: {exc}")
                )
        interrupt_ms = (time.perf_counter() - started) * 1000.0
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
        )
        remaining = (
            ()
            if gone
            else describe(observed, [child for child in spawned if self._probe.is_alive(child)])
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

    async def events(self) -> AsyncIterator[BridgeEvent]:
        while True:
            event = await self._events.get()
            if event is _EVENT_STREAM_END:
                return
            yield event

    async def _initialize(self) -> None:
        await self._transport.request(
            "initialize",
            {
                "clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION},
                "capabilities": {},
            },
            timeout=self.config.startup_timeout_seconds,
        )
        await self._transport.notify("initialized")

    async def _open_thread(self, resume: str | None) -> None:
        if resume:
            try:
                result = await self._transport.request(
                    "thread/resume",
                    {"threadId": resume},
                    timeout=self.config.startup_timeout_seconds,
                )
            except (JsonRpcError, TransportClosedError) as exc:
                self._emit(
                    SessionFaulted(
                        turn_id=None,
                        error=f"resume of thread {resume} failed: {exc}; starting a new thread",
                    )
                )
                self._resumed_existing = False
            else:
                thread = as_object(result.get("thread"))
                self._session_id = str(thread.get("id") or resume)
                self._resumed_existing = True
                self._emit(
                    SessionStarted(
                        session_id=self._session_id,
                        model=as_text(result.get("model")) or as_text(thread.get("model")),
                        cwd=as_text(result.get("cwd")) or str(self.config.cwd),
                        turn_id=None,
                    )
                )
                return
        params: dict[str, Any] = {
            "cwd": str(self.config.cwd),
            "approvalPolicy": self.config.approval_policy,
            "sandbox": self.config.sandbox,
        }
        if self.config.model:
            params["model"] = self.config.model
        result = await self._transport.request(
            "thread/start", params, timeout=self.config.startup_timeout_seconds
        )
        thread = as_object(result.get("thread"))
        self._session_id = str(thread.get("id") or "")
        self._emit(
            SessionStarted(
                session_id=self._session_id,
                model=as_text(result.get("model")) or as_text(thread.get("model")),
                cwd=as_text(thread.get("cwd")) or str(self.config.cwd),
                turn_id=None,
            )
        )

    def _resume_from_registry(self) -> str | None:
        if self._registry is None or self.agent_id is None:
            return None
        record = self._registry.load(self.agent_id)
        if record is None:
            return None
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
            model=self.config.model or "",
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
        self._baseline = snapshot(self._probe, self._transport.pid)
        self._start_deadline(ticket)
        try:
            result = await self._transport.request(
                "turn/start",
                {
                    "threadId": self._session_id,
                    "input": [{"type": "text", "text": ticket.text}],
                },
                timeout=self.config.startup_timeout_seconds,
            )
        except Exception as exc:
            self._fail_turn(ticket, exc)
            return
        turn = as_object(result.get("turn"))
        codex_turn_id = as_text(turn.get("id"))
        if codex_turn_id and ticket.turn_id not in self._turn_ids.values():
            self._turn_ids.setdefault(codex_turn_id, ticket.turn_id)

    def _start_deadline(self, ticket: TurnTicket) -> None:
        async def expire() -> None:
            await asyncio.sleep(self.config.turn_timeout_seconds)
            if self._active is not ticket:
                return
            self._emit(
                SessionFaulted(
                    turn_id=ticket.turn_id,
                    error=(f"turn exceeded {self.config.turn_timeout_seconds:.0f}s; interrupting"),
                )
            )
            try:
                await self.interrupt()
            except Exception as exc:
                self._emit(SessionFaulted(turn_id=ticket.turn_id, error=f"interrupt failed: {exc}"))

        self._deadlines[ticket.turn_id] = self._spawn(expire())

    def _on_notification(self, method: str, params: JsonObject) -> None:
        if method == "item/agentMessage/delta":
            self._on_text_delta(params)
        elif method in {"item/started", "item/completed"}:
            self._on_item(method, params)
        elif method == "thread/tokenUsage/updated":
            self._on_usage(params)
        elif method == "turn/completed":
            self._handle_turn_completed(params)
        elif method == "error":
            self._on_error(params)

    def _on_text_delta(self, params: JsonObject) -> None:
        turn_id = self._turn_id_for(params)
        if turn_id is None:
            return
        for event in text_delta(params, turn_id):
            self._emit(event)

    def _on_item(self, method: str, params: JsonObject) -> None:
        turn_id = self._turn_id_for(params)
        if turn_id is None:
            return
        item = as_object(params.get("item"))
        if method == "item/completed":
            text = assistant_text(item)
            if text is not None:
                self._record_text(turn_id, item, text)
                self._emit(AssistantText(turn_id=turn_id, text=text))
        phase = "started" if method == "item/started" else "completed"
        for event in item_event(params, phase=phase, turn_id=turn_id):
            self._emit(event)

    def _on_usage(self, params: JsonObject) -> None:
        turn_id = self._turn_id_for(params)
        if turn_id is not None:
            self._turn_usage[turn_id] = usage_from(params)

    def _on_error(self, params: JsonObject) -> None:
        if bool(params.get("willRetry", False)):
            return
        turn_id = self._turn_id_for(params)
        error = as_object(params.get("error")).get("message")
        self._emit(
            SessionFaulted(turn_id=turn_id, error=str(error or "app-server reported an error"))
        )

    def _record_text(self, turn_id: str, item: JsonObject, text: str) -> None:
        ticket = self._ticket_for(turn_id)
        if ticket is None:
            return
        ticket.parts.append(text)
        if is_final_answer(item):
            self._final_parts.setdefault(turn_id, []).append(text)

    def _handle_turn_completed(self, params: JsonObject) -> None:
        turn = as_object(params.get("turn"))
        codex_turn_id = as_text(turn.get("id")) or ""
        turn_id = self._turn_ids.pop(codex_turn_id, None)
        ticket = self._active
        if ticket is None or (turn_id is not None and ticket.turn_id != turn_id):
            self._emit(
                SessionFaulted(
                    turn_id=turn_id,
                    error=f"turn/completed for no active turn ({codex_turn_id or 'unknown'})",
                )
            )
            return
        fields = parse_turn(turn)
        usage = self._turn_usage.pop(ticket.turn_id, Usage())
        finals = self._final_parts.pop(ticket.turn_id, [])
        text = "\n".join(finals or ticket.parts).strip()
        interrupted = ticket.interrupt_requested or fields.status == "interrupted"
        is_error = fields.status in {"failed", "interrupted"} or fields.error_message is not None
        outcome = TurnOutcome(
            turn_id=ticket.turn_id,
            text=text,
            subtype=fields.status,
            is_error=is_error,
            interrupted=interrupted,
            stop_reason=fields.error_message,
            usage=usage,
            cost_usd=None,
            permission_denials=tuple(ticket.denials),
        )
        self._totals = self._totals.with_turn(usage, None)
        self._emit(
            TurnCompleted(
                turn_id=ticket.turn_id,
                session_id=self._session_id,
                text=text,
                subtype=fields.status,
                is_error=is_error,
                interrupted=interrupted,
                stop_reason=fields.error_message,
                num_turns=1,
                duration_ms=fields.duration_ms,
                usage=usage,
                cost_usd=None,
                cumulative_cost_usd=None,
                permission_denials=tuple(ticket.denials),
            )
        )
        self._complete(ticket, outcome)

    async def _on_request(self, method: str, params: JsonObject) -> dict[str, object]:
        decision = self.config.approval_decision
        if method in _INTERRUPT_METHODS and decision == "decline" and self._active is not None:
            self._active.denials.append(approval_denial(method, params))
        if method in _INTERRUPT_METHODS:
            if method == "applyPatchApproval":
                return {"decision": "approved" if decision == "accept" else "denied"}
            return {"decision": decision}
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn"}
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        return {}

    def _on_transport_closed(self, reason: str) -> None:
        if self._closed:
            return
        self._emit(SessionFaulted(turn_id=None, error=f"app-server: {reason}"))
        self._fail_active(TransportClosedError(reason))

    def _turn_id_for(self, params: JsonObject) -> str | None:
        codex_turn_id = as_text(params.get("turnId"))
        if codex_turn_id is None:
            return None
        known = self._turn_ids.get(codex_turn_id)
        if known is not None:
            return known
        ticket = self._active
        if ticket is None or ticket.turn_id in self._turn_ids.values():
            return None
        self._turn_ids[codex_turn_id] = ticket.turn_id
        return ticket.turn_id

    def _codex_turn_for(self, turn_id: str) -> str:
        for codex_turn_id, mapped in self._turn_ids.items():
            if mapped == turn_id:
                return codex_turn_id
        return ""

    def _ticket_for(self, turn_id: str) -> TurnTicket | None:
        active = self._active
        if active is not None and active.turn_id == turn_id:
            return active
        return None

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
        self._fail_pending(SessionClosedError(f"session failed: {exc}"))

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
