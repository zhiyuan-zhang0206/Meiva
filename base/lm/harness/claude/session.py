from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from base.lm.harness.claude.auth import build_env
from base.lm.harness.claude.config import BridgeConfig
from base.lm.harness.claude.mapping import map_message, parse_result
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
    client_pid,
    describe,
    snapshot,
    wait_gone,
)
from base.lm.harness.registry import SessionRecord, SessionRegistry

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
_EVENT_STREAM_END = object()


class InboundQueueFullError(RuntimeError):
    pass


class MissingClaudeSdkError(RuntimeError):
    pass


def _load_sdk() -> Any:
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


class ClaudeSession:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        environ: Mapping[str, str],
        agent_id: int | None = None,
        registry: SessionRegistry | None = None,
        sdk: Any | None = None,
        probe: ChildProbe | None = None,
    ) -> None:
        self.config = config
        self.agent_id = agent_id
        self._sdk: Any = sdk if sdk is not None else _load_sdk()
        self._probe: ChildProbe = probe or PsutilChildProbe()
        self._registry = registry
        self._environ = dict(environ)
        self._events: asyncio.Queue[Any] = asyncio.Queue()
        self._pending: deque[TurnTicket] = deque()
        self._completed: OrderedDict[str, TurnOutcome] = OrderedDict()
        self._active: TurnTicket | None = None
        self._client: Any | None = None
        self._reader: asyncio.Task[None] | None = None
        self._deadlines: dict[str, asyncio.Task[None]] = {}
        self._tasks = asyncio.TaskGroup()
        self._children: set[asyncio.Task[Any]] = set()
        self._tasks_entered = False
        self._totals = SessionTotals()
        self._session_id: str | None = None
        self._cli_pid: int | None = None
        self._baseline: dict[int, str] = {}
        self._closed = False
        self._dropped_deltas = 0
        self._resumed_existing = False
        self._record: SessionRecord | None = None
        self._handoff_ticket: TurnTicket | None = None

    async def start(self, *, resume: str | None = None, handoff: Handoff | None = None) -> None:
        if self._client is not None:
            raise RuntimeError("session already started")
        if self._closed:
            raise SessionClosedError("session is closed")
        resume = resume or self._resume_from_registry()
        client = self._sdk.ClaudeSDKClient(options=self._build_options(resume))
        await client.connect()
        self._client = client
        self._cli_pid = client_pid(client)
        if not self._tasks_entered:
            await self._tasks.__aenter__()
            self._tasks_entered = True
        self._reader = self._spawn(self._read_loop())
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
        if self._client is not None:
            await self._client.disconnect()
        self._emit(SessionClosed(turn_id=None, reason=reason))
        self._events.put_nowait(_EVENT_STREAM_END)

    async def __aenter__(self) -> ClaudeSession:
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
        return self._cli_pid

    @property
    def totals(self) -> SessionTotals:
        return self._totals

    @property
    def dropped_deltas(self) -> int:
        return self._dropped_deltas

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
        started = time.perf_counter()
        await client.interrupt()
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

    async def events(self) -> AsyncIterator[BridgeEvent]:
        while True:
            event = await self._events.get()
            if event is _EVENT_STREAM_END:
                return
            yield event

    def _build_options(self, resume: str | None) -> Any:
        return self._sdk.ClaudeAgentOptions(
            model=self.config.model,
            cwd=str(self.config.cwd),
            tools=list(self.config.tools),
            permission_mode=self.config.permission_mode,
            max_turns=self.config.max_turns,
            setting_sources=[],
            strict_mcp_config=True,
            include_partial_messages=True,
            env=build_env(self._environ, self.config.auth),
            resume=resume,
        )

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
        client = self._require_client()
        ticket.started_at = time.monotonic()
        self._baseline = snapshot(self._probe, self._cli_pid)
        self._start_deadline(ticket)
        try:
            await client.query(ticket.text)
        except Exception as exc:
            self._fail_turn(ticket, exc)

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

    async def _read_loop(self) -> None:
        client = self._require_client()
        try:
            async for message in client.receive_messages():
                await self._handle(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(SessionFaulted(turn_id=None, error=f"session stream failed: {exc}"))
            self._fail_active(exc)

    async def _handle(self, message: Any) -> None:
        name = type(message).__name__
        if name == "ResultMessage":
            self._handle_result(message)
            return
        if name in {"RateLimitEvent", "SystemMessage"}:
            for event in map_message(message, turn_id=None):
                if isinstance(event, SessionStarted):
                    self._session_id = event.session_id or self._session_id
                    self._emit(event)
                    self._bind()
                    continue
                self._emit(event)
            return
        ticket = self._active
        if ticket is None:
            return
        for event in map_message(message, turn_id=ticket.turn_id):
            if isinstance(event, AssistantText):
                ticket.parts.append(event.text)
            self._emit(event)

    def _handle_result(self, message: Any) -> None:
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
        self._emit(
            TurnCompleted(
                turn_id=ticket.turn_id,
                session_id=fields.session_id or self._session_id,
                text=text,
                subtype=fields.subtype,
                is_error=fields.is_error,
                interrupted=ticket.interrupt_requested,
                stop_reason=fields.stop_reason,
                num_turns=fields.num_turns,
                duration_ms=fields.duration_ms,
                usage=fields.usage,
                cost_usd=fields.cost_usd,
                cumulative_cost_usd=self._totals.cost_usd,
                permission_denials=fields.permission_denials,
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

    def _require_client(self) -> Any:
        if self._client is None:
            raise SessionClosedError("session is not started")
        return self._client
