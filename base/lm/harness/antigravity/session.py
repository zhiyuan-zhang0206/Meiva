from __future__ import annotations

import asyncio
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
from base.lm.harness.antigravity.timing_refresh import TimingRefresh
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
    background_overlap: bool = False
    keys: list[str] = field(default_factory=list[str])
    requests: dict[int, str] = field(default_factory=dict[int, str])
    latest: int = 0
    reports: dict[int, PromptUsage] = field(default_factory=dict[int, PromptUsage])
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
        self._retirement: asyncio.Task[None] | None = None
        self._retirement_error: Exception | None = None
        self._background_changed = asyncio.Event()
        self._prompt_turn: _PromptTurn | None = None
        self._tools: dict[str, ToolCall] = {}
        self._text: list[str] = []
        self._idle_text: list[str] = []
        self._adopted: set[str] = set()
        self._schedules = Schedules(
            max_timer_seconds=config.max_schedule_timer_seconds,
            reply_wait_seconds=config.background_reply_seconds,
        )
        self._subagents = SubagentReader(self._home)
        self._timing_refresh = TimingRefresh(
            clock, self._spawn, agent_id=self.agent_id, interval=timing_refresh_interval
        )

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
        self._retirement = None
        self._session_id = session_id
        self._model = self.config.model
        self._transport = transport
        self._emit(SessionStarted(session_id, self._model, str(self.config.cwd)))
        self._bind()
        for frame in transport.buffered_events():
            self._handle(transport, frame)
        self._reader = self._spawn(self._read_loop(transport, generation))
        _ = self._spawn(self._watch(transport, generation))
        return transport

    async def close(self, *, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        self._timing_refresh.cancel()
        if self._prompt_turn is not None:
            self._prompt_turn.finished.set()
        if (ticket := self._active) is not None:
            if ticket.started_at is None:
                self._apply(self._ledger.rejected(ticket.input_key, reason))
            ticket.interrupt_ready.set()
        self._background_changed.set()
        self._active = self._prompt_turn = None
        transport, self._transport = self._transport, None
        try:
            await self._await_retirement()
            if transport is not None and self._retirement is None:
                await self._close_transport(transport)
        finally:
            await self._close_tasks()
            self._finish_close(reason)

    async def _close_transport(self, transport: AgyTransport, *, broken: bool = False) -> None:
        watched = watch_tree(self._probe, transport.pid)
        await transport.close(broken=broken)
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
        for entry in validated_batch(inputs):
            transport = await self._dispatch(entry)
            if transport is not None:
                await self._flush_prompt(transport)
            if self._closed:
                raise SessionClosedError("session is closed")
            if self._transport_failed:
                return

    async def _dispatch(self, entry: HarnessInput) -> AgyTransport | None:
        await self._await_background_reply()
        self._ledger = self._ledger.sent(entry.key)
        ticket = self._active
        if ticket is None:
            self._flush_idle_text()
            ticket = self._start_input_turn(entry)
            await ticket.interrupt_ready.wait()
            return (
                self._transport
                if ticket.started_at is not None and not self._transport_failed
                else None
            )
        turn, transport = self._prompt_turn, self._transport
        if turn is None or transport is None or ticket.interrupt_requested:
            self._apply(self._ledger.rejected(entry.key, "agy is not accepting input"))
            return None
        try:
            self._send_prompt(transport, turn, entry)
        except (TransportClosedError, OSError) as exc:
            self._fail_transport(transport, exc)
        return transport

    async def _await_background_reply(self) -> None:
        while not self._closed:
            self._background_changed.clear()
            deadline = self._schedules.reply_deadline(self._clock.now().timestamp())
            if deadline is None:
                return
            try:
                async with asyncio.timeout(max(0.0, deadline - self._clock.now().timestamp())):
                    await self._background_changed.wait()
                if self._transport_failed:
                    raise SessionClosedError(
                        "agy ACP session failed while waiting for a background reply"
                    )
            except TimeoutError as exc:
                if self._transport is not None:
                    self._fail_transport(
                        self._transport, RuntimeError("agy ACP background reply timed out")
                    )
                raise SessionClosedError("agy ACP background reply timed out") from exc
        raise SessionClosedError("session is closed")

    async def _flush_prompt(self, transport: AgyTransport) -> None:
        if self._transport is not transport or self._transport_failed or self._closed:
            return
        try:
            await transport.flush_prompt()
        except (TimeoutError, TransportClosedError, OSError) as exc:
            self._fail_transport(
                transport, RuntimeError(f"agy ACP prompt write failed: {exc or type(exc).__name__}")
            )

    async def _begin(self, ticket: ActiveTurn) -> None:
        transport = self._transport
        try:
            if transport is None or self._transport_failed:
                await self._await_retirement()
                transport = await self._open_transport(self._session_id)
            await self._await_background_reply()
            self._baseline = self._tool_processes(transport)
            turn = _PromptTurn(ticket, len(self._transcript()))
            self._prompt_turn = turn
            ticket.started_at = self._monotonic()
            self._send_prompt(
                transport, turn, HarnessInput(ticket.input_key, ticket.blocks), starts_turn=True
            )
        except (OSError, RuntimeError, TimeoutError) as exc:
            if self._active is ticket:
                if transport is not None and self._prompt_turn is not None:
                    self._fail_transport(transport, exc)
                else:
                    self._apply(
                        self._ledger.rejected(ticket.input_key, f"agy ACP startup failed: {exc}")
                    )
                    self._fail_turn(ticket, exc)
        finally:
            ticket.interrupt_ready.set()

    def _send_prompt(
        self,
        transport: AgyTransport,
        turn: _PromptTurn,
        entry: HarnessInput,
        *,
        starts_turn: bool = False,
    ) -> None:
        request = transport.next_request_id()
        turn.keys.append(entry.key)
        turn.requests[request] = entry.key
        turn.latest = request
        self._ledger = self._ledger.scoped(entry.key, turn.ticket.turn_id)
        transport.prompt(request, prompt_blocks(entry.key, entry.blocks))
        if starts_turn:
            self._emit(
                TurnStarted(turn.ticket.turn_id, TurnOrigin.INPUT, (entry.key,), self.generation)
            )
        self._apply(self._ledger.received(entry.key, AckSignal.WEAK))

    def _tool_processes(self, transport: AgyTransport) -> dict[int, str]:
        pid = transport.pid
        return self._probe.tool_processes(pid) if pid is not None else {}

    async def interrupt(self, *, turn_id: str | None = None) -> CancelReport:
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
            async with asyncio.timeout(self.config.cancel_settle_seconds):
                await turn.finished.wait()
        except (TimeoutError, OSError, TransportClosedError) as exc:
            self._fail_transport(
                transport, RuntimeError(f"agy ACP cancel unconfirmed: {exc or type(exc).__name__}")
            )
        interrupt_ms = (self._monotonic() - started) * 1000
        if not turn.interrupted:
            await self._await_retirement()
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
                if (
                    self._transport is transport
                    and not self._transport_failed
                    and self._ledger.accepts(generation)
                ):
                    self._handle(transport, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
        finally:
            if not self._closed:
                self._fail_transport(transport, error)

    def _fail_transport(self, transport: AgyTransport, error: BaseException) -> None:
        if self._transport is not transport or self._transport_failed or self._closed:
            return
        self._transport_failed = True
        self._flush_idle_text()
        self._timing_refresh.cancel()
        self._lose_schedules()
        self._background_changed.set()
        self._tools.clear()
        self._emit(SessionFailed(error=str(error)))
        self._fail_prompt_turn(error)
        self._retirement = self._spawn(self._retire_transport(transport))

    async def _retire_transport(self, transport: AgyTransport) -> None:
        try:
            await self._close_transport(transport, broken=True)
        except (RuntimeError, OSError, TimeoutError) as exc:
            self._retirement_error = exc
            self._emit(SessionFailed(error=str(exc)))

    async def _await_retirement(self) -> None:
        if self._retirement is not None:
            await asyncio.shield(self._retirement)
        if self._retirement_error is not None:
            raise self._retirement_error

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
        background = bool(self._schedules.replies)
        kind = update.get("sessionUpdate")
        text = content_text(update.get("content"))
        if kind == "user_message_chunk" and text and turn is not None:
            self._ack_markers(turn, {text.split("\n", 1)[0]})
        elif kind == "agent_message_chunk" and text:
            self._record_text(None if background else turn, text)
        elif kind == "agent_thought_chunk" and text and turn is not None and not background:
            self._record_thought(turn.ticket, text)
        elif kind in ("tool_call", "tool_call_update"):
            self._tool_update(None if background else turn, update)
        elif kind == "usage_update":
            self._finish_output_batch(turn, background=background)

    def _record_text(self, turn: _PromptTurn | None, text: str) -> None:
        if turn is None:
            self._idle_text.append(text)
            return
        self._flush_thoughts(turn.ticket)
        self._text.append(text)
        self._emit(TextDelta(turn.ticket.turn_id, text))

    def _record_thought(self, ticket: ActiveTurn, text: str) -> None:
        if self._text:
            self._flush_text(ticket)
        ticket.thoughts.append(text)
        self._emit(ReasoningDelta(ticket.turn_id, text))

    def _finish_output_batch(self, turn: _PromptTurn | None, *, background: bool) -> None:
        if turn is not None and not background:
            self._flush_text(turn.ticket)
        elif self._idle_text:
            self._flush_idle_text()
            if background:
                self._schedules = self._schedules.reply_finished()
                self._schedule_changed()
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
            turn.reports[request] = usage
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
        usage, steps = self._bill_turn(turn, rows)
        self._totals = self._totals.with_turn(usage, None)
        turn.interrupted = reason == "cancelled"
        if turn.interrupted:
            self._cancel_tools(ticket.turn_id)
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
            ticket.turn_id, now=now, monotonic_now=self._monotonic(), cancelled=turn.interrupted
        )
        self._schedule_changed()
        self._prompt_turn = None
        self._complete(ticket)
        turn.finished.set()

    def _bill_turn(
        self, turn: _PromptTurn, rows: list[dict[str, object]]
    ) -> tuple[Usage, tuple[Usage, ...]]:
        final = turn.ticket.parts[-1] if turn.ticket.parts else ""
        steps = (
            ()
            if turn.background_overlap
            else planner_usage(rows[turn.transcript_offset :], turn.keys, final)
        )
        bill = turn_usage(
            [turn.reports[request] for request in sorted(turn.reports)],
            steps,
            request_count=len(turn.keys),
            terminal_reported=turn.latest in turn.reports,
        )
        if bill.uncertain:
            logger.warning(
                "Antigravity ACP usage is incomplete or ambiguous for {turn_id}",
                turn_id=turn.ticket.turn_id,
            )
        return bill.usage, steps

    def _cancel_tools(self, turn_id: str) -> None:
        for tool_id, call in tuple(self._tools.items()):
            if call.use.turn_id == turn_id:
                del self._tools[tool_id]
                result = ToolResult(turn_id, tool_id, is_error=True, content="Cancelled")
                self._emit(result)
                self._track_schedule(result)

    def _fail_prompt_turn(self, exc: BaseException) -> None:
        turn, self._prompt_turn = self._prompt_turn, None
        self._text.clear()
        if turn is not None:
            turn.finished.set()
        self._fail_active(exc)

    def _transcript(self) -> list[dict[str, object]]:
        return transcript_rows(self._session_id, home=self._home) if self._session_id else []

    async def _watch(self, transport: AgyTransport, generation: int) -> None:
        while self._ledger.accepts(generation) and self._live_transport() is transport:
            await self._clock.asleep(TRANSCRIPT_POLL_SECONDS)
            if self._live_transport() is not transport:
                return
            now = self._clock.now().timestamp()
            deadline = self._schedules.reply_deadline(now)
            if deadline is not None and deadline <= now:
                self._fail_transport(transport, RuntimeError("agy ACP background reply timed out"))
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
        if len(self._schedules.replies) > len(before.replies) and self._prompt_turn is not None:
            self._flush_text(self._prompt_turn.ticket)
            self._prompt_turn.background_overlap = True
        if self._schedules is not before:
            self._schedule_changed()

    def _schedule_changed(self) -> None:
        self._persist_background(self._schedules.pending_at(self._clock.now().timestamp()))
        self._background_changed.set()

    def _lose_schedules(self) -> None:
        lost = self._schedules.pending_at(self._clock.now().timestamp())
        self._schedules = Schedules(
            max_timer_seconds=self.config.max_schedule_timer_seconds,
            reply_wait_seconds=self.config.background_reply_seconds,
        )
        self._lose_background(lost)

    def background_work(self) -> BackgroundWork:
        if self._closed:
            return BackgroundWork()
        now = self._clock.now().timestamp()
        return BackgroundWork(self._schedules.pending_at(now))

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

    def request_timing_refresh(self) -> None:
        transport = self._live_transport()
        if transport is not None:
            self._timing_refresh.request(transport, lambda: self._live_transport() is transport)


__all__ = ["EVENT_STREAM_END", "ActiveTurn", "AgySession", "CancelReport", "SessionClosedError"]
