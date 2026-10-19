from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from base.lm.harness.acp.transport import (
    AcpClosedError,
    AcpConnection,
    AcpError,
    AcpNotification,
    AcpRpcError,
    AcpTimeoutError,
    PermissionRequest,
)
from base.lm.harness.deepseek.config import BridgeConfig
from base.lm.harness.deepseek.mapping import assistant_text, map_update, parse_prompt_result
from base.lm.harness.deepseek.types import (
    CancelReport,
    SessionTotals,
    TurnOutcome,
    TurnTicket,
)
from base.lm.harness.deepseek.usage import UsageSnapshot, read_usage
from base.lm.harness.events import (
    BridgeEvent,
    HandoffInjected,
    SessionBound,
    SessionClosed,
    SessionFaulted,
    SessionStarted,
    TextDelta,
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
from base.lm.pricing import PriceBook, quote

# meiva:note
# One session is one ACP connection with a single in-flight prompt, because the
# runtime refuses a second prompt on a session (`-32602 a prompt is already in
# flight`). Inbound is queued here and promoted one at a time; a full queue is
# refused rather than buffered, and a repeated key returns the ticket already
# issued.
#
# Cancellation is a notification plus an assertion. `session/cancel` settles the
# turn with `stopReason: cancelled`, but the runtime does not reap what that turn
# spawned: a background command it started, or anything a `setsid` escaped, keeps
# running. `interrupt()` sweeps the runtime's descendants, kills what the turn
# started, and reports `settled=False` with the survivors named. The bridge owes
# this because the model can no longer see those processes once the turn ends.
#
# A turn's `session/update` notifications and its `session/prompt` response travel
# on one wire but reach this object through two queues, so settling yields one
# event-loop turn first: a chunk the transport already delivered is then in the
# turn's parts before the answer text is computed, instead of racing the outcome.
# meiva:link base/lm/harness/deepseek/docs/deepseek.meiva.okf.md
CANCEL_POLL_SECONDS = 0.05
_EVENT_STREAM_END = object()


class InboundQueueFullError(RuntimeError):
    pass


class SessionClosedError(RuntimeError):
    pass


class DeepSeekSession:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        environ: Mapping[str, str],
        agent_id: int | None = None,
        registry: SessionRegistry | None = None,
        probe: ChildProbe | None = None,
        connect: Any | None = None,
        kill: Callable[[int, int], None] | None = None,
        prices: PriceBook | None = None,
    ) -> None:
        self.config = config
        self._prices = prices
        self.agent_id = agent_id
        self._kill = kill or os.kill
        self._environ = dict(environ)
        self._probe: ChildProbe = probe or PsutilChildProbe()
        self._registry = registry
        self._connect = connect or self.open_connection
        self._events: asyncio.Queue[Any] = asyncio.Queue(maxsize=config.max_buffered_events)
        self._pending: deque[TurnTicket] = deque()
        self._completed: OrderedDict[str, TurnOutcome] = OrderedDict()
        self._active: TurnTicket | None = None
        self._connection: AcpConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._totals = SessionTotals()
        self._session_id: str | None = None
        self._agent_pid: int | None = None
        self._baseline: dict[int, str] = {}
        self._closed = False
        self._dropped_events = 0
        self._resumed_existing = False
        self._record: SessionRecord | None = None
        self._handoff_ticket: TurnTicket | None = None
        self._permission_denials: list[str] = []
        self._usage: UsageSnapshot | None = None
        self._context_used: int | None = None
        self._context_size: int | None = None
        self._tasks = asyncio.TaskGroup()
        self._children: set[asyncio.Task[Any]] = set()
        self._tasks_entered = False
        self._residue: tuple[ProcessObservation, ...] = ()

    async def start(self, *, resume: str | None = None, handoff: Handoff | None = None) -> None:
        if self._connection is not None:
            raise RuntimeError("session already started")
        if self._closed:
            raise SessionClosedError("session is closed")
        connection = self._connect()
        self._connection = connection
        await connection.start()
        self._agent_pid = connection.pid
        await connection.request(
            "initialize",
            {"protocolVersion": 1, "clientCapabilities": {}},
            timeout=self.config.startup_timeout_seconds,
        )
        resume = resume or self._resume_from_registry()
        if resume:
            await connection.request(
                "session/resume",
                {
                    "sessionId": resume,
                    "cwd": self.config.workspace,
                    "mcpServers": list(self.config.mcp_servers),
                },
                timeout=self.config.startup_timeout_seconds,
            )
            self._session_id = resume
        else:
            created = await connection.request(
                "session/new",
                {"cwd": self.config.workspace, "mcpServers": list(self.config.mcp_servers)},
                timeout=self.config.startup_timeout_seconds,
            )
            session_id = created.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise AcpError(f"session/new returned no sessionId: {created!r}")
            self._session_id = session_id
        await self._configure()
        if not self._tasks_entered:
            await self._tasks.__aenter__()
            self._tasks_entered = True
        self._reader = self._spawn(self._read_loop(connection))
        self._emit(
            SessionStarted(
                session_id=self._session_id,
                model=self.config.model,
                cwd=self.config.workspace,
                turn_id=None,
            )
        )
        self._bind()
        self._inject_handoff(handoff)

    async def close(self, *, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        self._fail_pending(SessionClosedError("session closed"))
        connection = self._connection
        if connection is not None:
            if self._session_id is not None and not connection.closed:
                with contextlib.suppress(AcpError, AcpClosedError):
                    await connection.request(
                        "session/close",
                        {"sessionId": self._session_id},
                        timeout=self.config.cancel_settle_seconds,
                    )
            await connection.close(reason=reason)
        await self._close_tasks()
        self._residue = await self._sweep(reason="session closed")
        self._emit(SessionClosed(turn_id=None, reason=reason))
        self._events.put_nowait(_EVENT_STREAM_END)

    async def __aenter__(self) -> DeepSeekSession:
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
    def agent_pid(self) -> int | None:
        return self._agent_pid

    @property
    def resumed(self) -> bool:
        return self._resumed_existing

    @property
    def record(self) -> SessionRecord | None:
        return self._record

    @property
    def totals(self) -> SessionTotals:
        return self._totals

    @property
    def usage(self) -> UsageSnapshot | None:
        return self._usage

    @property
    def residue(self) -> tuple[ProcessObservation, ...]:
        return self._residue

    @property
    def handoff_ticket(self) -> TurnTicket | None:
        return self._handoff_ticket

    @property
    def dropped_events(self) -> int:
        return self._dropped_events

    @property
    def context_occupancy(self) -> tuple[int | None, int | None]:
        return (self._context_used, self._context_size)

    async def events(self) -> AsyncIterator[BridgeEvent]:
        while True:
            event = await self._events.get()
            if event is _EVENT_STREAM_END:
                return
            yield event

    async def interrupt(self) -> CancelReport:
        connection = self._require_connection()
        ticket = self._active
        if ticket is not None:
            ticket.interrupt_requested = True
        pid = self._agent_pid
        if pid is None:
            return CancelReport(
                turn_id=ticket.turn_id if ticket is not None else None,
                cancel_ms=0.0,
                settled=None,
                observed=(),
                remaining=(),
                waited_ms=0.0,
                agent_pid=None,
                agent_alive=None,
            )
        observed = snapshot(self._probe, pid)
        spawned = {
            child: command for child, command in observed.items() if child not in self._baseline
        }
        started = time.perf_counter()
        if self._session_id is not None:
            await connection.notify("session/cancel", {"sessionId": self._session_id})
        settle_deadline = time.monotonic() + self.config.cancel_settle_seconds
        while ticket is not None and not ticket.settled and time.monotonic() < settle_deadline:
            await asyncio.sleep(CANCEL_POLL_SECONDS)
        cancel_ms = (time.perf_counter() - started) * 1000.0
        reaped = await self._reap(spawned)
        gone, waited_ms = await wait_gone(
            self._probe,
            reaped,
            timeout_seconds=self.config.cancel_settle_seconds,
            poll_seconds=CANCEL_POLL_SECONDS,
        )
        remaining = describe(observed, [child for child in reaped if self._probe.is_alive(child)])
        return CancelReport(
            turn_id=ticket.turn_id if ticket is not None else None,
            cancel_ms=cancel_ms,
            settled=True if not reaped else gone,
            observed=describe(observed, spawned),
            remaining=remaining,
            waited_ms=waited_ms,
            agent_pid=pid,
            agent_alive=self._probe.is_alive(pid),
        )

    async def _reap(self, spawned: Mapping[int, str]) -> tuple[int, ...]:
        reaped: list[int] = []
        for child in sorted(spawned):
            if child == self._agent_pid or not self._probe.is_alive(child):
                continue
            with contextlib.suppress(ProcessLookupError, PermissionError):
                self._kill(child, signal.SIGTERM)
            reaped.append(child)
        if reaped:
            await asyncio.sleep(CANCEL_POLL_SECONDS)
        for child in reaped:
            if self._probe.is_alive(child):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    self._kill(child, signal.SIGKILL)
        return tuple(reaped)

    async def _sweep(self, *, reason: str) -> tuple[ProcessObservation, ...]:
        pid = self._agent_pid
        observed = snapshot(self._probe, pid)
        survivors = {
            child: command for child, command in observed.items() if child not in self._baseline
        }
        if not survivors:
            return ()
        await self._reap(survivors)
        still = [child for child in survivors if self._probe.is_alive(child)]
        if still:
            self._emit(
                SessionFaulted(
                    turn_id=None,
                    error=f"{len(still)} descendant(s) survived {reason}; inspect before reuse",
                )
            )
        return describe(observed, still)

    def open_connection(self) -> AcpConnection:
        return AcpConnection(
            [self.config.binary, "--profile", self.config.profile],
            cwd=self.config.workspace,
            environ=self._environment(),
            permission=self.permission_decision,
            request_timeout_seconds=self.config.turn_timeout_seconds,
            shutdown_timeout_seconds=self.config.shutdown_timeout_seconds,
        )

    def _environment(self) -> dict[str, str]:
        env = dict(self._environ)
        env["DSH_HOME"] = str(self.config.dsh_home)
        env.update(self.config.env_overrides)
        return env

    async def permission_decision(self, request: PermissionRequest) -> str | None:
        mode = self.config.permission_mode
        if mode == "allow":
            option = request.option("allow_once")
            return option.option_id if option else None
        denial = request.option("reject_once")
        self._permission_denials.append(
            str(request.tool_call.get("title") or request.tool_call.get("toolCallId") or "tool")
        )
        return denial.option_id if denial else None

    async def _configure(self) -> None:
        connection = self._require_connection()
        session_id = self._session_id
        if session_id is None or self.config.model is None:
            return
        with contextlib.suppress(AcpRpcError):
            await connection.request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": "model", "value": self.config.model},
                timeout=self.config.startup_timeout_seconds,
            )
        if self.config.reasoning_effort is not None:
            with contextlib.suppress(AcpRpcError):
                await connection.request(
                    "session/set_config_option",
                    {
                        "sessionId": session_id,
                        "configId": "reasoning_effort",
                        "value": self.config.reasoning_effort,
                    },
                    timeout=self.config.startup_timeout_seconds,
                )

    async def _read_loop(self, connection: AcpConnection) -> None:
        try:
            async for notification in connection.notifications():
                self._dispatch(notification)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(SessionFaulted(turn_id=None, error=f"session stream failed: {exc}"))
            self._fail_active(exc)

    def _dispatch(self, notification: AcpNotification) -> None:
        if notification.method != "session/update":
            return
        params = notification.params
        if params.get("sessionId") != self._session_id:
            return
        raw_update = params.get("update")
        if not isinstance(raw_update, dict):
            return
        update = cast("dict[str, Any]", raw_update)
        if update.get("sessionUpdate") == "usage_update":
            used, size = update.get("used"), update.get("size")
            self._context_used = used if isinstance(used, int) else self._context_used
            self._context_size = size if isinstance(size, int) else self._context_size
            return
        ticket = self._active
        if ticket is None:
            return
        for event in map_update(update, turn_id=ticket.turn_id):
            if isinstance(event, TextDelta):
                ticket.parts.append(event.text)
            self._emit(event)

    async def _begin(self, ticket: TurnTicket) -> None:
        connection = self._require_connection()
        ticket.started_at = time.monotonic()
        self._baseline = snapshot(self._probe, self._agent_pid)
        try:
            result = await connection.request(
                "session/prompt",
                {
                    "sessionId": self._session_id,
                    "prompt": [{"type": "text", "text": ticket.text}],
                },
                timeout=self.config.turn_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except AcpTimeoutError as exc:
            self._emit(
                SessionFaulted(
                    turn_id=ticket.turn_id,
                    error=f"turn exceeded {self.config.turn_timeout_seconds:.0f}s; interrupting",
                )
            )
            with contextlib.suppress(AcpError):
                await self.interrupt()
            self._fail_turn(ticket, exc)
            return
        except AcpError as exc:
            self._fail_turn(ticket, exc)
            return
        await self._settle_turn(ticket, result)

    async def _settle_turn(self, ticket: TurnTicket, result: Mapping[str, Any]) -> None:
        await asyncio.sleep(0)
        fields = parse_prompt_result(result)
        text = "\n".join(ticket.parts).strip()
        usage, measured = await self._turn_usage()
        cost = self._turn_cost(usage) if measured else None
        self._totals = self._totals.with_turn(usage, measured=measured, cost=cost)
        outcome = TurnOutcome(
            turn_id=ticket.turn_id,
            text=text,
            subtype=fields.subtype,
            is_error=fields.is_error,
            interrupted=ticket.interrupt_requested or fields.interrupted,
            stop_reason=fields.stop_reason,
            usage=usage,
            cost_usd=cost,
            permission_denials=tuple(self._permission_denials),
            per_turn_usage=measured,
        )
        self._permission_denials.clear()
        if self._active is ticket:
            self._active = None
        if ticket.key is not None:
            self._remember(ticket.key, outcome)
        assistant = assistant_text(text, turn_id=ticket.turn_id)
        if assistant is not None:
            self._emit(assistant)
        self._emit(
            TurnCompleted(
                turn_id=ticket.turn_id,
                session_id=self._session_id,
                text=text,
                subtype=outcome.subtype,
                is_error=outcome.is_error,
                interrupted=outcome.interrupted,
                stop_reason=outcome.stop_reason,
                num_turns=len(ticket.parts),
                duration_ms=int(
                    (time.monotonic() - (ticket.started_at or time.monotonic())) * 1000
                ),
                usage=usage,
                cost_usd=cost,
                cumulative_cost_usd=self._totals.cost_usd if self._totals.cost_known else None,
                permission_denials=outcome.permission_denials,
            )
        )
        ticket.settle(outcome)
        self._touch()
        self._promote()

    async def _turn_usage(self) -> tuple[Usage, bool]:
        session_id = self._session_id
        if session_id is None:
            return (Usage(), False)
        observed = await asyncio.to_thread(
            read_usage, self.config.dsh_home, self.config.cwd, session_id
        )
        self._usage = observed
        if not observed.per_turn_usage:
            return (Usage(), False)
        steps = observed.latest_turn()
        return (
            Usage(
                input_tokens=sum(step.input_tokens for step in steps),
                output_tokens=sum(step.output_tokens for step in steps),
                cache_creation_input_tokens=sum(step.cache_write_tokens for step in steps),
                cache_read_input_tokens=sum(step.cache_read_tokens for step in steps),
            ),
            True,
        )

    def _turn_cost(self, usage: Usage) -> float | None:
        model = self.config.model
        if self._prices is None or model is None:
            return None
        quoted = quote(
            model,
            usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens,
            usage.output_tokens,
            usage.cache_read_input_tokens,
            prices=self._prices,
        )
        return None if quoted is None else quoted.cost_usd

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
            cwd=self.config.workspace,
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

    def _recall(self, key: str) -> TurnTicket | None:
        outcome = self._completed.get(key)
        if outcome is None:
            return None
        future: asyncio.Future[TurnOutcome] = asyncio.get_running_loop().create_future()
        future.set_result(outcome)
        return TurnTicket(
            turn_id=outcome.turn_id, text="", key=key, future=future, deduplicated=True
        )

    def _remember(self, key: str, outcome: TurnOutcome) -> None:
        self._completed[key] = outcome
        while len(self._completed) > self.config.dedupe_window:
            self._completed.popitem(last=False)

    def _promote(self) -> None:
        if self._closed or self._active is not None or not self._pending:
            return
        ticket = self._pending.popleft()
        self._active = ticket
        self._spawn(self._begin(ticket))

    def _fail_turn(self, ticket: TurnTicket, exc: BaseException) -> None:
        self._emit(SessionFaulted(turn_id=ticket.turn_id, error=f"turn failed: {exc}"))
        ticket.fail(SessionClosedError(f"turn failed: {exc}"))
        if self._active is ticket:
            self._active = None
        self._promote()

    def _fail_active(self, exc: BaseException) -> None:
        ticket = self._active
        if ticket is None:
            return
        self._active = None
        ticket.fail(SessionClosedError(f"turn failed: {exc}"))
        self._fail_pending(SessionClosedError(f"session failed: {exc}"))

    def _fail_pending(self, exc: BaseException) -> None:
        while self._pending:
            self._pending.popleft().fail(exc)
        active = self._active
        if active is not None:
            self._active = None
            active.fail(exc)

    def _emit(self, event: BridgeEvent) -> None:
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            if is_droppable(event):
                self._dropped_events += 1
                return
            if self._drain_one_droppable():
                self._events.put_nowait(event)
            else:
                self._dropped_events += 1

    def _drain_one_droppable(self) -> bool:
        kept: list[Any] = []
        found = False
        while True:
            try:
                item = self._events.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not found and is_droppable(item):
                found = True
                continue
            kept.append(item)
        for item in kept:
            self._events.put_nowait(item)
        return found

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = self._tasks.create_task(coro)
        self._children.add(task)
        task.add_done_callback(self._children.discard)
        return task

    async def _close_tasks(self) -> None:
        children = [task for task in self._children if not task.done()]
        for task in children:
            task.cancel()
        if children:
            await asyncio.gather(*children, return_exceptions=True)
        if self._tasks_entered:
            self._tasks_entered = False
            with contextlib.suppress(ExceptionGroup):
                await self._tasks.__aexit__(None, None, None)

    def _require_connection(self) -> AcpConnection:
        if self._connection is None:
            raise SessionClosedError("session not started")
        return self._connection


def dsh_home_for(workspace: Path) -> Path:
    return Path(workspace) / ".dsh"
