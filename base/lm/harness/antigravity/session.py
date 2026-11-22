from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.host.env.harness_binaries import resolve_harness_binary
from base.lm.harness.antigravity.acp import (
    ToolCall,
    content_text,
    input_marker,
    prompt_blocks,
    received_markers,
)
from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.mapping import transcript_rows
from base.lm.harness.antigravity.reconciliation.queue_files import agy_home, reconcile_acp_inputs
from base.lm.harness.antigravity.schedules import Schedules
from base.lm.harness.antigravity.subagent_files import SubagentReader
from base.lm.harness.antigravity.subagents import INVOKE_SUBAGENT
from base.lm.harness.antigravity.transport import AgyTransport, TransportClosedError
from base.lm.harness.antigravity.usage import PromptUsage, planner_usage, turn_usage
from base.lm.harness.base_session import (
    EVENT_STREAM_END,
    ActiveTurn,
    BaseHarnessSession,
    SessionBinding,
    SessionClock,
)
from base.lm.harness.cancel import CancelReport, Sweep, observed_cancel, unobserved_cancel
from base.lm.harness.delivery import HarnessInput, InputPhase, Reconciliation, validated_batch
from base.lm.harness.events import (
    AckSignal,
    AssistantText,
    InterruptConfirmed,
    ReasoningDelta,
    ReasoningText,
    SessionFailed,
    SessionStarted,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
    UnsolicitedOutput,
    Usage,
)
from base.lm.harness.process import ChildProbe, settle_close, wait_gone, watch_tree
from base.lm.harness.session import BackgroundWork, SessionClosedError, SessionTotals
from base.log import logger

TRANSCRIPT_POLL_SECONDS = 0.5
TIMING_REFRESH_MIN_INTERVAL_SECONDS = 1.0
CANCEL_POLL_SECONDS = 0.02
STOP_REASONS = frozenset({"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"})


@dataclass(slots=True)
class _PromptTurn:
    ticket: ActiveTurn
    transcript_offset: int
    keys: list[str] = field(default_factory=list[str])
    requests: dict[int, str] = field(default_factory=dict[int, str])
    latest: int = 0
    reports: list[PromptUsage] = field(default_factory=list[PromptUsage])
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False


# meiva:note
# One process serves the conversation. A foreground turn owns the initial ACP prompt and every
# steer sent before its boundary; replacing the outstanding prompt does not end the foreground.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
class AgySession(BaseHarnessSession[AntigravityConfig]):
    harness = "antigravity"

    def __init__(
        self,
        config: AntigravityConfig,
        *,
        environ: Mapping[str, str] | None = None,
        binding: SessionBinding,
        probe: ChildProbe | None = None,
        transport_factory: Callable[[], AgyTransport] | None = None,
        clock: SessionClock,
        home: Path | None = None,
        timing_refresh_interval: float = TIMING_REFRESH_MIN_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(
            config,
            binding=binding,
            probe=probe,
            totals=SessionTotals(cost_usd=None),
            clock=clock,
        )
        self._environ = dict(environ) if environ else None
        # meiva:note
        # The user home the conversation transcript is read from; None is the real one.
        self._home = (
            agy_home(home, environ)
            if home is not None or (environ and environ.get("HOME"))
            else None
        )
        self._factory = transport_factory or self._default_transport
        self._transport: AgyTransport | None = None
        self._transport_failed = False
        self._dispatch_lock = asyncio.Lock()
        self._prompt_turn: _PromptTurn | None = None
        self._tools: dict[str, ToolCall] = {}
        self._text: list[str] = []
        self._idle_text: list[str] = []
        self._adopted: set[str] = set()
        self._schedules = Schedules(max_timer_seconds=config.max_schedule_timer_seconds)
        self._subagents = SubagentReader(self._home)
        self._timing_refresh_interval = timing_refresh_interval
        self._timing_refresh_task: asyncio.Task[None] | None = None
        self._timing_refresh_pending = False
        self._timing_refresh_last: float | None = None

    def _read_records(
        self, session_id: str, keys: Sequence[str]
    ) -> dict[str, Reconciliation] | None:
        return reconcile_acp_inputs(agy_home(self._home, self._environ), session_id, keys)

    def adopt(self, keys: Sequence[str]) -> None:
        super().adopt(keys)
        self._adopted.update(keys)

    async def start(self, *, resume: str | None = None) -> None:
        if self._transport is not None:
            raise RuntimeError("session already started")
        if self._closed:
            raise SessionClosedError("session is closed")
        resume = self._resolve_resume(resume)
        if resume:
            self._subagents.adopt(resume)
        await self._enter_tasks()
        try:
            await self._open_transport(resume)
        except BaseException:
            await self._close_tasks()
            raise

    async def _open_transport(self, resume: str | None) -> AgyTransport:
        generation = self._begin_generation()
        transport = self._factory()
        try:
            await transport.start()
            session_id = await transport.open_session(resume=resume)
        except BaseException:
            await transport.close()
            raise
        if self._closed:
            await transport.close()
            raise SessionClosedError("session is closed")
        self._transport_failed = False
        self._session_id = session_id
        self._model = self.config.model
        self._transport = transport
        self._emit(SessionStarted(session_id, self._model, str(self.config.cwd)))
        self._bind()
        self._reader = self._spawn(self._read_loop(transport, generation))
        _ = self._spawn(self._watch(transport, generation))
        return transport

    async def close(self, *, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_timing_refresh()
        if self._prompt_turn is not None:
            self._prompt_turn.finished.set()
        self._active = self._prompt_turn = None
        transport, self._transport = self._transport, None
        await self._close_tasks()
        if transport is not None:
            await self._close_transport(transport)
        self._finish_close(reason)

    async def _close_transport(self, transport: AgyTransport) -> None:
        watched = watch_tree(self._probe, transport.pid)
        await transport.close()
        await settle_close(
            self._probe,
            watched,
            "antigravity",
            self.config.cancel_settle_seconds,
            agent_id=self.agent_id,
            clock=self._clock,
        )
        self._exit_code = transport.returncode

    @property
    def cli_pid(self) -> int | None:
        return self._transport.pid if self._transport is not None else None

    def _default_transport(self) -> AgyTransport:
        resolved = resolve_harness_binary(
            self.config.executable[0], path_env=(self._environ or {}).get("PATH")
        )
        return AgyTransport(
            replace(self.config, executable=(resolved, *self.config.executable[1:])),
            environ=self._environ,
            clock=self._clock,
        )

    async def deliver(self, inputs: Sequence[HarnessInput]) -> None:
        batch = validated_batch(inputs)
        async with self._dispatch_lock:
            if self._closed:
                raise SessionClosedError("session is closed")
            for entry in batch:
                await self._deliver_one(entry)

    async def _deliver_one(self, entry: HarnessInput) -> None:
        self._ledger = self._ledger.sent(entry.key)
        ticket = self._active
        if ticket is None:
            self._flush_idle_text()
            ticket = self._start_input_turn(entry)
            await ticket.interrupt_ready.wait()
            return
        turn, transport = self._prompt_turn, self._transport
        if turn is None or transport is None or ticket.interrupt_requested:
            self._apply(self._ledger.rejected(entry.key, "agy is not accepting input"))
            return
        try:
            self._send_prompt(transport, turn, entry)
            await transport.flush()
        except (TransportClosedError, OSError) as exc:
            self._apply(self._ledger.rejected(entry.key, f"agy ACP write failed: {exc}"))

    async def _begin(self, ticket: ActiveTurn) -> None:
        ticket.started_at = self._monotonic()
        key = ticket.input_key
        try:
            transport = self._transport
            if transport is None or self._transport_failed:
                if transport is not None:
                    await self._close_transport(transport)
                transport = await self._open_transport(self._session_id)
            self._baseline = self._tool_processes(transport)
            turn = _PromptTurn(ticket, len(self._transcript()))
            self._prompt_turn = turn
            self._send_prompt(transport, turn, HarnessInput(key, ticket.blocks), starts_turn=True)
            ticket.interrupt_ready.set()
            await transport.flush()
        except (OSError, RuntimeError, TimeoutError) as exc:
            self._apply(self._ledger.rejected(key, f"agy ACP write failed: {exc}"))
            if self._active is ticket:
                self._fail_prompt_turn(exc)

    def _send_prompt(
        self,
        transport: AgyTransport,
        turn: _PromptTurn,
        entry: HarnessInput,
        *,
        starts_turn: bool = False,
    ) -> None:
        request = transport.next_request_id()
        transport.prompt(request, prompt_blocks(entry.key, entry.blocks))
        turn.keys.append(entry.key)
        turn.requests[request] = entry.key
        turn.latest = request
        self._ledger = self._ledger.scoped(entry.key, turn.ticket.turn_id)
        if starts_turn:
            self._emit(
                TurnStarted(turn.ticket.turn_id, TurnOrigin.INPUT, (entry.key,), self.generation)
            )
        self._apply(self._ledger.received(entry.key, AckSignal.WEAK))

    def _tool_processes(self, transport: AgyTransport) -> dict[int, str]:
        pid = transport.pid
        return self._probe.tool_processes(pid) if pid is not None else {}

    async def interrupt(self, *, turn_id: str | None = None) -> CancelReport:
        async with self._dispatch_lock:
            ticket = await self._interrupt_target(turn_id)
            transport, turn = self._transport, self._prompt_turn
            pid = transport.pid if transport is not None else None
            if ticket is None or turn is None or transport is None or pid is None:
                return unobserved_cancel(
                    turn_id=turn_id, cli_pid=pid, cli_alive=transport.alive if transport else None
                )
            observed = self._tool_processes(transport)
            spawned = {pid_: cmd for pid_, cmd in observed.items() if pid_ not in self._baseline}
            started = self._monotonic()
            try:
                transport.cancel()
            except TransportClosedError:
                return unobserved_cancel(
                    turn_id=ticket.turn_id, cli_pid=pid, cli_alive=transport.alive
                )
        # A late response still owns the foreground. A new prompt would replace that request.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(turn.finished.wait(), timeout=self.config.cancel_settle_seconds)
        interrupt_ms = (self._monotonic() - started) * 1000
        if not turn.interrupted:
            return unobserved_cancel(
                turn_id=ticket.turn_id,
                cli_pid=pid,
                cli_alive=transport.alive,
                interrupt_ms=interrupt_ms,
            )
        gone, waited_ms = await wait_gone(
            self._probe,
            spawned,
            timeout_seconds=self.config.cancel_settle_seconds,
            poll_seconds=CANCEL_POLL_SECONDS,
            clock=self._clock,
        )
        return observed_cancel(
            turn_id=ticket.turn_id,
            interrupt_ms=interrupt_ms,
            pid=pid,
            cli_alive=transport.alive,
            sweep=Sweep(
                observed,
                spawned,
                gone,
                waited_ms,
                tuple(child for child in spawned if self._probe.is_alive(child)),
            ),
        )

    async def _read_loop(self, transport: AgyTransport, generation: int) -> None:
        error: BaseException = SessionClosedError("agy ACP stream ended")
        try:
            async for payload in transport.events():
                if self._transport is transport and self._ledger.accepts(generation):
                    self._handle(transport, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
        finally:
            if self._transport is transport:
                self._flush_idle_text()
                self._transport_failed = True
                self._cancel_timing_refresh()
                self._lose_schedules()
                self._tools.clear()
                self._emit(SessionFailed(error=str(error)))
                self._fail_prompt_turn(error)

    def _handle(self, transport: AgyTransport, payload: dict[str, object]) -> None:
        method = payload.get("method")
        request = payload.get("id")
        params = payload.get("params")
        if method is None and type(request) is int:
            self._handle_result(request, payload)
        elif method == "session/update" and is_object_dict(params):
            update = params.get("update")
            if params.get("sessionId") == self._session_id and is_object_dict(update):
                self._handle_update(update)
        elif isinstance(request, (int, str)):
            if method == "session/request_permission" and is_object_dict(params):
                self._deny_permission(transport, request, params)
            else:
                transport.respond_error(request)

    def _deny_permission(
        self,
        transport: AgyTransport,
        request: int | str,
        params: dict[str, object],
    ) -> None:
        ticket = self._active
        if ticket is not None and params.get("sessionId") == self._session_id:
            call = params.get("toolCall")
            title = call.get("title") if is_object_dict(call) else None
            ticket.denials.append(title if isinstance(title, str) else "tool permission")
        transport.reject_permission(request, params)

    def _handle_update(self, update: dict[str, object]) -> None:
        turn = self._prompt_turn
        kind = update.get("sessionUpdate")
        text = content_text(update.get("content"))
        if kind == "user_message_chunk" and text and turn is not None:
            self._ack_markers(turn, {text.split("\n", 1)[0]})
        elif kind == "agent_message_chunk" and text:
            if turn is None:
                self._idle_text.append(text)
            else:
                self._flush_thoughts(turn.ticket)
                self._text.append(text)
                self._emit(TextDelta(turn.ticket.turn_id, text))
        elif kind == "agent_thought_chunk" and text and turn is not None:
            if self._text:
                self._flush_text(turn.ticket)
            turn.ticket.thoughts.append(text)
            self._emit(ReasoningDelta(turn.ticket.turn_id, text))
        elif kind in ("tool_call", "tool_call_update"):
            self._tool_update(turn, update)
        elif kind == "usage_update":
            if turn is None:
                self._flush_idle_text()
            else:
                self._flush_text(turn.ticket)
            self.request_timing_refresh()

    def _tool_update(self, turn: _PromptTurn | None, update: dict[str, object]) -> None:
        tool_id = update.get("toolCallId")
        if not isinstance(tool_id, str) or update.get("name") == INVOKE_SUBAGENT:
            return
        call = self._tools.get(tool_id)
        if call is None:
            if turn is None or update.get("sessionUpdate") != "tool_call":
                return
            self._flush_text(turn.ticket)
            call = ToolCall.from_update(update, turn_id=turn.ticket.turn_id, tool_id=tool_id)
            self._tools[tool_id] = call
            self._emit(call.use)
            self._track_schedule(call.use)
        result = call.result(update)
        if result is not None:
            del self._tools[tool_id]
            self._emit(result)
            self._track_schedule(result)

    def _flush_text(self, ticket: ActiveTurn) -> None:
        self._flush_thoughts(ticket)
        text = "".join(self._text).strip()
        self._text.clear()
        if text:
            ticket.parts.append(text)
            self._emit(AssistantText(ticket.turn_id, text))
            self._schedules = self._schedules.on_assistant_text(
                ticket.turn_id, monotonic_now=self._monotonic()
            )

    def _flush_thoughts(self, ticket: ActiveTurn) -> None:
        text = "".join(ticket.thoughts).strip()
        ticket.thoughts.clear()
        if text:
            self._emit(ReasoningText(ticket.turn_id, text))

    def _flush_idle_text(self) -> None:
        text = "".join(self._idle_text).strip()
        self._idle_text.clear()
        if text:
            self._unsolicited(UnsolicitedOutput(source="session/update", text=text))

    def _handle_result(self, request: int, response: dict[str, object]) -> None:
        turn = self._prompt_turn
        if turn is None or request not in turn.requests:
            return
        result = response.get("result")
        reason = result.get("stopReason") if is_object_dict(result) else None
        rows = self._transcript()
        self._ack_markers(turn, received_markers(rows))
        key = turn.requests.pop(request)
        if not isinstance(reason, str) or reason not in STOP_REASONS or not is_object_dict(result):
            error = f"agy ACP prompt failed: {response.get('error', reason)!r}"
            self._apply(self._ledger.rejected(key, error))
            if request == turn.latest:
                if turn.requests:
                    turn.latest = max(turn.requests)
                else:
                    self._fail_prompt_turn(RuntimeError(error))
            return
        if (usage := PromptUsage.from_result(result.get("usage"))) is not None:
            turn.reports.append(usage)
        if request != turn.latest:
            return
        if reason != "cancelled":
            self._ack_input(turn, key)
        self._finish_turn(turn, reason, rows)

    def _ack_markers(self, turn: _PromptTurn, marks: set[str]) -> None:
        for key in (*turn.keys, *self._adopted):
            if input_marker(key) in marks:
                self._ack_input(turn, key)

    def _ack_input(self, turn: _PromptTurn, key: str) -> None:
        if self._ledger.phase(key) is InputPhase.IN_CONTEXT:
            return
        self._apply(self._ledger.in_context(key, turn.ticket.turn_id))
        self._adopted.discard(key)
        self._schedules = self._schedules.on_input(
            turn.ticket.turn_id, monotonic_now=self._monotonic()
        )

    def _finish_turn(
        self,
        turn: _PromptTurn,
        reason: str,
        rows: list[dict[str, object]],
    ) -> None:
        ticket = turn.ticket
        self._flush_text(ticket)
        self._show_subagents(ticket.turn_id)
        self._apply(self._ledger.lose_unsettled(ticket.turn_id))
        steps = planner_usage(rows[turn.transcript_offset :])
        usage = turn_usage(turn.reports, steps)
        self._totals = self._totals.with_turn(usage, None)
        turn.interrupted = reason == "cancelled"
        subtype = "interrupted" if turn.interrupted else "denied" if ticket.denials else "success"
        self._emit(
            TurnCompleted(
                turn_id=ticket.turn_id,
                session_id=self._session_id,
                text="\n".join(ticket.parts),
                subtype=subtype,
                is_error=reason != "end_turn" or bool(ticket.denials),
                interrupted=turn.interrupted,
                stop_reason=reason,
                num_turns=max(len(steps), 1),
                duration_ms=int((self._monotonic() - (ticket.started_at or 0)) * 1000),
                usage=usage,
                cost_usd=None,
                cumulative_cost_usd=self._totals.cost_usd,
                permission_denials=tuple(ticket.denials),
                steps=steps if sum(steps, Usage()) == usage else (),
                generation=self.generation,
            )
        )
        if turn.interrupted:
            self._emit(InterruptConfirmed(ticket.turn_id, self.generation))
        self.request_timing_refresh()
        now = self._clock.now().timestamp()
        self._schedules = self._schedules.finish_turn(
            ticket.turn_id, now=now, monotonic_now=self._monotonic()
        )
        self._persist_background(self._schedules.pending_at(now))
        self._prompt_turn = None
        self._complete(ticket)
        turn.finished.set()

    def _fail_prompt_turn(self, exc: BaseException) -> None:
        turn, self._prompt_turn = self._prompt_turn, None
        self._text.clear()
        if turn is not None:
            self._apply(self._ledger.lose_unsettled(turn.ticket.turn_id))
            turn.finished.set()
        self._fail_active(exc)

    def _transcript(self) -> list[dict[str, object]]:
        return transcript_rows(self._session_id, home=self._home) if self._session_id else []

    async def _watch(self, transport: AgyTransport, generation: int) -> None:
        while self._ledger.accepts(generation) and self._live_transport() is transport:
            await self._clock.asleep(TRANSCRIPT_POLL_SECONDS)
            if self._live_transport() is not transport:
                return
            turn = self._prompt_turn
            if turn is not None:
                self._show_subagents(turn.ticket.turn_id)
                if any(self._ledger.phase(key) is InputPhase.RECEIVED for key in turn.keys):
                    self._ack_markers(turn, received_markers(self._transcript()))
            if self._adopted:
                await self._ack_adopted(transport)

    async def _ack_adopted(self, transport: AgyTransport) -> None:
        try:
            trajectory = await transport.trajectory(conversation_id=self._session_id)
        except (OSError, RuntimeError):
            return
        keys = self._adopted.intersection(trajectory.client_message_ids())
        if self._live_transport() is not transport or not keys:
            return
        turn = self._active
        turn_id = turn.turn_id if turn is not None else self._mint_turn_id()
        for key in keys:
            self._apply(self._ledger.in_context(key, turn_id))
        self._adopted.difference_update(keys)

    def _show_subagents(self, turn_id: str) -> None:
        if not self._session_id:
            return
        events = self._subagents.poll(self._session_id, turn_id)
        if events:
            for event in events:
                self._emit(event)
            self._schedules = self._schedules.on_wakeup(turn_id, monotonic_now=self._monotonic())

    def _track_schedule(self, event: ToolUse | ToolResult) -> None:
        before = self._schedules
        mono = self._monotonic()
        if isinstance(event, ToolUse):
            self._schedules = before.tool_used(
                event.tool_use_id,
                event.name,
                event.tool_input,
                turn_id=event.turn_id,
                monotonic_now=mono,
            )
        elif isinstance(event, ToolResult):
            self._schedules = before.tool_returned(
                event.tool_use_id,
                is_error=event.is_error,
                now=self._clock.now().timestamp(),
                content=event.content,
                monotonic_now=mono,
            )
        if self._schedules is not before:
            self._persist_background(self._schedules.pending_at(self._clock.now().timestamp()))

    def _lose_schedules(self) -> None:
        lost = self._schedules.pending_at(self._clock.now().timestamp())
        self._schedules = Schedules(max_timer_seconds=self.config.max_schedule_timer_seconds)
        self._lose_background(lost)

    def background_work(self) -> BackgroundWork:
        if self._closed:
            return BackgroundWork()
        now = self._clock.now().timestamp()
        return BackgroundWork((*self._schedules.pending_at(now), *self._observed_turn_items()))

    def _live_transport(self) -> AgyTransport | None:
        t = self._transport
        if (
            self._closed
            or self._transport_failed
            or not self._session_id
            or t is None
            or not t.alive
        ):
            return None
        return t

    def silence_floor_monotonic(self, turn_ids: Collection[str]) -> float | None:
        return None if self._closed else self._schedules.silence_floor_monotonic(turn_ids)

    async def waiting_on_background(self, turn_ids: Collection[str]) -> bool:
        t, ticket = self._live_transport(), self._active
        if t is None or ticket is None or set(turn_ids) != {ticket.turn_id}:
            return False
        try:
            snap = await t.trajectory(conversation_id=self._session_id)
        except Exception:
            logger.opt(exception=True).warning(
                "agent {agent_id}: background wait trajectory check failed", agent_id=self.agent_id
            )
            return False
        if self._active is not ticket or self._live_transport() is not t:
            return False
        if snap.is_reactive_background_idle():
            return True
        return snap.is_idle_without_planner() and self._schedules.has_active_wait(
            ticket.turn_id, now=self._clock.now().timestamp(), monotonic_now=self._monotonic()
        )

    # meiva:note
    # Single-flight, trailing-throttled background `GetCascadeTrajectory` collector for native
    # reasoning timing. Triggered by ACP usage updates, turn completion, and newly persisted
    # `PLANNER_RESPONSE` rows while reactive-IDLE
    # so `meiva-thinking-timing.json` stays current without blocking live timeline delivery or
    # piling up concurrent RPCs.
    # meiva:link base/lm/harness/antigravity/docs/antigravity.md
    def request_timing_refresh(self) -> None:
        transport = self._live_transport()
        if transport is None or not transport.has_timing_record(conversation_id=self._session_id):
            return
        self._timing_refresh_pending = True
        task = self._timing_refresh_task
        if task is not None and not task.done():
            return
        self._timing_refresh_task = self._spawn(
            self._run_timing_refresh(transport, self.generation)
        )

    def _cancel_timing_refresh(self) -> None:
        self._timing_refresh_pending = False
        task, self._timing_refresh_task = self._timing_refresh_task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            _ = task.cancel()

    def _timing_refresh_wait_seconds(self) -> float:
        last = self._timing_refresh_last
        return (
            0.0
            if last is None
            else max(0.0, self._timing_refresh_interval - (self._monotonic() - last))
        )

    def _timing_refresh_active(self, transport: AgyTransport, generation: int) -> bool:
        return (
            self._live_transport() is transport
            and self._ledger.accepts(generation)
            and self._session_id is not None
        )

    async def _run_timing_refresh(self, transport: AgyTransport, generation: int) -> None:
        try:
            while self._timing_refresh_pending and self._timing_refresh_active(
                transport, generation
            ):
                wait = self._timing_refresh_wait_seconds()
                if wait > 0.0:
                    await self._clock.asleep(wait)
                    if not self._timing_refresh_active(transport, generation):
                        return
                self._timing_refresh_pending = False
                self._timing_refresh_last = self._monotonic()
                try:
                    _ = await transport.trajectory(conversation_id=self._session_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not self._timing_refresh_active(transport, generation):
                        return
                    logger.warning(
                        "agent {agent_id}: background timing refresh failed ({error}); "
                        "provider execution is unchanged",
                        event="harness_timing_collection_failed",
                        phase="refresh",
                        agent_id=self.agent_id,
                        error=type(exc).__name__,
                    )
                    return
        finally:
            if self._timing_refresh_task is asyncio.current_task():
                self._timing_refresh_task = None


__all__ = ["EVENT_STREAM_END", "ActiveTurn", "AgySession", "CancelReport", "SessionClosedError"]
