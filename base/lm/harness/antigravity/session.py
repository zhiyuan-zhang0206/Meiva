from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.lm.harness.antigravity.acp import content_text, prompt_blocks
from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.foreground import PromptTurn, PromptUsage, ToolUpdates
from base.lm.harness.antigravity.mapping import (
    IdleReplies,
    NativeReply,
    OutputProjection,
    TranscriptSnapshot,
    text_update,
)
from base.lm.harness.antigravity.reconciliation.queue_files import agy_home, reconcile_acp_inputs
from base.lm.harness.antigravity.schedules import Schedules
from base.lm.harness.antigravity.subagent_files import SubagentReader
from base.lm.harness.antigravity.timing_refresh import TimingRefresh
from base.lm.harness.antigravity.transcript.input_store import record_prompt, settle_inputs
from base.lm.harness.antigravity.transport import AgyTransport, TransportClosedError
from base.lm.harness.base_session import (
    EVENT_STREAM_END,
    ActiveTurn,
    BaseHarnessSession,
    SessionBinding,
    SessionClock,
)
from base.lm.harness.cancel import CancelReport, Sweep, observed_cancel, unobserved_cancel
from base.lm.harness.delivery import (
    HarnessInput,
    InputPhase,
    InputPreparation,
    Reconciliation,
    validated_batch,
)
from base.lm.harness.events import (
    AckSignal,
    AssistantText,
    InterruptConfirmed,
    SessionFailed,
    SessionStarted,
    ToolResult,
    ToolUse,
    TurnOrigin,
    TurnStarted,
)
from base.lm.harness.process import ChildProbe, wait_gone
from base.lm.harness.session import (
    BackgroundWork,
    InputsNotWrittenError,
    SessionClosedError,
    SessionTotals,
)
from base.log import logger

TRANSCRIPT_POLL_SECONDS = 0.5
TIMING_REFRESH_MIN_INTERVAL_SECONDS = 1.0
CANCEL_POLL_SECONDS = 0.02
STOP_REASONS = frozenset({"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"})


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
        transport_factory: Callable[[], AgyTransport],
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
        self._home = (
            agy_home(home, environ)
            if home is not None or (environ and environ.get("HOME"))
            else None
        )
        self._factory = transport_factory
        self._transport: AgyTransport | None = None
        self._transport_failed = False
        self._recycle_when_idle = False
        self._retirement: asyncio.Task[None] | None = None
        self._retirement_error: Exception | None = None
        self._background_changed = asyncio.Event()
        self._prompt_turn: PromptTurn | None = None
        self._projection: OutputProjection | None = None
        self._tools = ToolUpdates()
        self._idle_replies = IdleReplies(0, config.max_buffered_events)
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
        if self._session_id is not None:
            raise RuntimeError("session already started")
        if self._closed:
            raise SessionClosedError("session is closed")
        resume = self._resolve_resume(resume)
        self._subagents.adopt(resume)
        await self._enter_tasks()
        try:
            await self._open_transport(resume)
        except BaseException:
            await self._close_tasks()
            raise

    # meiva:note
    # agy can replay history after setup, so every loaded generation projects its first prompt.
    async def _open_transport(self, resume: str | None) -> AgyTransport:
        generation = self._begin_generation()
        transport = self._factory()
        self._resume_projection = resume is not None
        offset = len(self._transcript(resume).rows) if resume else 0
        self._idle_replies = IdleReplies(offset, self.config.max_buffered_events)
        try:
            await transport.start(agent_id=self.agent_id)
            session_id = await transport.open_session(resume=resume)
        except BaseException:
            await self._close_transport(transport, broken=True)
            raise
        if self._closed:
            await transport.close()
            raise SessionClosedError("session is closed")
        self._transport_failed = False
        self._retirement = None
        self._session_id = session_id
        self._idle_replies.bind(session_id, home=self._home)
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
        await transport.close(broken=broken, agent_id=self.agent_id)
        self._exit_code = transport.returncode

    @property
    def cli_pid(self) -> int | None:
        return self._transport.pid if self._transport is not None else None

    async def deliver(self, inputs: Sequence[HarnessInput]) -> None:
        await self.deliver_with_opening(inputs)

    async def deliver_with_opening(
        self, inputs: Sequence[HarnessInput], prepare: InputPreparation | None = None
    ) -> None:
        batch = validated_batch(inputs)
        for entry in batch:
            self._ledger = self._ledger.sent(entry.key)
        try:
            await self._deliver_batch(batch, prepare)
        except SessionClosedError as exc:
            unwritten = frozenset(entry.key for entry in batch if self._unwritten(entry.key))
            if not unwritten:
                raise
            for key in unwritten:
                self._apply(self._ledger.rejected(key, str(exc)))
            raise InputsNotWrittenError(str(exc), unwritten) from exc

    async def _deliver_batch(
        self, batch: Sequence[HarnessInput], prepare: InputPreparation | None
    ) -> None:
        for index, entry in enumerate(batch):
            transport = await self._dispatch(entry, prepare)
            if transport is not None:
                await self._flush_prompt(transport)
            if self._closed:
                raise SessionClosedError("session is closed")
            if self._transport_failed:
                if index + 1 < len(batch) or self._unwritten(entry.key):
                    raise SessionClosedError("agy ACP delivery stopped")
                return

    def _unwritten(self, key: str) -> bool:
        return self._ledger.scope_of(key) is None and self._ledger.phase(key) in (
            InputPhase.SENT,
            InputPhase.REJECTED,
        )

    async def reconcile(self, keys: Sequence[str]) -> dict[str, Reconciliation]:
        unwritten = {key: Reconciliation.NOT_FOUND for key in keys if self._unwritten(key)}
        unresolved = [key for key in keys if key not in unwritten]
        persisted = await super().reconcile(unresolved) if unresolved else {}
        return {**persisted, **unwritten}

    async def _dispatch(
        self, entry: HarnessInput, prepare: InputPreparation | None
    ) -> AgyTransport | None:
        await self._await_background_reply()
        ticket = self._active
        if ticket is None:
            ticket = self._start_input_turn(entry, prepare=prepare)
            await ticket.interrupt_ready.wait()
            return self._transport if ticket.started_at is not None else None
        turn, transport = self._prompt_turn, self._transport
        if turn is None or transport is None or ticket.interrupt_requested:
            self._apply(self._ledger.rejected(entry.key, "agy is not accepting input"))
            return None
        try:
            self._send_prompt(transport, turn, prepare(entry) if prepare is not None else entry)
        except (TransportClosedError, OSError) as exc:
            self._retire_transport(transport, exc)
        return transport

    async def _await_background_reply(self) -> None:
        while not self._closed and self._prompt_turn is None:
            self._background_changed.clear()
            if self._transport is not None:
                self._replay_idle()
            now = self._clock.now().timestamp()
            draining = bool(self._projection and self._projection.has_undrained_native)
            if not self._schedules.blocks_prompt(now, draining_echo=draining):
                break
            await self._background_changed.wait()
            if self._transport_failed:
                raise SessionClosedError("agy ACP session failed during timer wait")
        if self._closed:
            raise SessionClosedError("session is closed")

    async def _flush_prompt(self, transport: AgyTransport) -> None:
        if self._transport is not transport or self._closed:
            return
        try:
            await transport.flush_prompt()
        except (TimeoutError, TransportClosedError, OSError) as exc:
            self._retire_transport(
                transport, RuntimeError(f"agy ACP prompt write failed: {exc or type(exc).__name__}")
            )

    async def _begin(self, ticket: ActiveTurn) -> None:
        transport = None
        try:
            await self._await_background_reply()
            if self._transport and self._projection and self._projection.has_undrained_native:
                self._retire_transport(self._transport)
            transport = self._transport
            if transport is None:
                await self._await_retirement()
                transport = await self._open_transport(self._session_id)
            pid = transport.pid
            self._baseline = self._probe.tool_processes(pid) if pid is not None else {}
            turn = PromptTurn(ticket, len(self._transcript().rows))
            if self._projection is None:
                required = self._resume_projection or self._subagents.has_children(self._session_id)
                self._projection = turn.require_projection(required=required)
            elif self._resume_projection:
                self._projection.fences_replay = True
            self._resume_projection = False
            self._prompt_turn = turn
            ticket.started_at = self._monotonic()
            self._send_prompt(transport, turn, ticket.prepare_input(), starts_turn=True)
        except (OSError, RuntimeError, TimeoutError) as exc:
            if self._active is ticket:
                if transport is not None and self._prompt_turn is not None:
                    self._retire_transport(transport, exc)
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
        turn: PromptTurn,
        entry: HarnessInput,
        *,
        starts_turn: bool = False,
    ) -> None:
        request = transport.next_request_id()
        turn.register_request(request, entry.key)
        if self._projection is not None:
            self._projection.keys.append(entry.key)
        blocks = prompt_blocks(entry.blocks)

        def writing() -> None:
            record_prompt(self._home, self._session_id, entry.key, blocks)
            self._ledger = self._ledger.scoped(entry.key, turn.ticket.turn_id)

        transport.prompt(request, blocks, before_write=writing)
        if starts_turn:
            self._emit(
                TurnStarted(turn.ticket.turn_id, TurnOrigin.INPUT, (entry.key,), self.generation)
            )
        self._apply(self._ledger.received(entry.key, AckSignal.WEAK))

    async def interrupt(self, *, turn_id: str | None = None) -> CancelReport:
        ticket = await self._interrupt_target(turn_id)
        transport, turn = self._transport, self._prompt_turn
        pid = transport.pid if transport is not None else None
        if ticket is None or turn is None or transport is None or pid is None:
            return unobserved_cancel(
                turn_id=turn_id, cli_pid=pid, cli_alive=transport.alive if transport else None
            )
        observed = self._probe.tool_processes(pid)
        spawned = {pid_: cmd for pid_, cmd in observed.items() if pid_ not in self._baseline}
        started = self._monotonic()
        try:
            transport.cancel()
            async with asyncio.timeout(self.config.cancel_settle_seconds):
                await turn.finished.wait()
        except (TimeoutError, OSError, TransportClosedError) as exc:
            self._retire_transport(
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
                if self._transport is transport and self._ledger.accepts(generation):
                    self._handle(transport, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
        finally:
            self._retire_transport(transport, error)

    def _retire_transport(
        self, transport: AgyTransport, error: BaseException | None = None
    ) -> None:
        if self._transport is not transport or self._closed:
            return
        self._transport = None
        self._transport_failed = error is not None
        self._recycle_when_idle = False
        self._timing_refresh.cancel()
        self._lose_schedules()
        self._background_changed.set()
        self._tools.clear()
        self._subagents.defer_replies(self._projection if error is None else None, self._session_id)
        self._projection = None
        self._idle_replies = IdleReplies(0, self.config.max_buffered_events)
        if error is not None:
            self._emit(SessionFailed(error=str(error)))
            self._fail_prompt_turn(error)
        self._retirement = self._spawn(self._close_retired(transport, broken=error is not None))

    async def _close_retired(self, transport: AgyTransport, *, broken: bool) -> None:
        try:
            await self._close_transport(transport, broken=broken)
        except (RuntimeError, OSError, TimeoutError) as exc:
            self._retirement_error = exc
            if broken:
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
            if method != "session/request_permission" or not is_object_dict(params):
                transport.respond_error(request)
                return
            if (ticket := self._active) is not None and params.get("sessionId") == self._session_id:
                call = params.get("toolCall")
                title = call.get("title") if is_object_dict(call) else None
                ticket.denials.append(title if isinstance(title, str) else "tool permission")
            transport.reject_permission(request, params)

    def _handle_update(self, update: dict[str, object]) -> None:
        turn, projection = self._prompt_turn, self._projection
        kind = update.get("sessionUpdate")
        if kind == "user_message_chunk" and turn is not None:
            self._ack_inputs(turn, self._transcript())
        elif kind == "usage_update":
            if turn is None:
                self._replay_idle()
            elif projection is not None:
                self._replay(projection, self._transcript())
            else:
                self._flush_text(turn)
            self.request_timing_refresh()
        elif kind in (
            "agent_message_chunk",
            "agent_thought_chunk",
            "tool_call",
            "tool_call_update",
        ):
            tool_id = update.get("toolCallId")
            if isinstance(tool_id, str) and tool_id in self._tools:
                self._project_update(turn, update)
            elif turn is None:
                self._idle_replies.note_frame(update)
            elif projection is None:
                self._project_update(turn, update)
            elif kind != "tool_call_update" or projection.expects_tool(tool_id):
                projection.pending.append(update)

    def _project_update(
        self, turn: PromptTurn | None, update: dict[str, object], *, finished: bool = False
    ) -> None:
        kind = update.get("sessionUpdate")
        text = content_text(update.get("content"))
        if kind == "agent_message_chunk" and text and turn is not None:
            for event in turn.record_text(text):
                self._emit(event)
            if finished:
                self._flush_text(turn)
        elif kind == "agent_thought_chunk" and text and turn is not None:
            if turn.text:
                self._flush_text(turn)
            self._emit(turn.record_thought(text))
        elif kind in ("tool_call", "tool_call_update"):
            self._tool_update(turn, update)

    def _replay(self, projection: OutputProjection, transcript: TranscriptSnapshot) -> None:
        rows, turn = transcript.rows, self._prompt_turn
        if turn is not None:
            turn.prove_projection(projection, rows)
        for item in projection.project(rows):
            if turn is not None and item.input_key in turn.keys:
                self._project_update(turn, item.frame, finished=item.text_finished)
        for reply in projection.background_replies(transcript, self._session_id, self._subagents):
            self._finish_background_reply(reply)
        if turn and (reason := turn.settle_projection(projection, rows, self._monotonic)):
            for text in turn.restore_verified_text(projection, rows):
                self._project_update(turn, text_update(text), finished=True)
            self._finish_turn(turn, reason, rows)

    def _finish_background_reply(self, reply: NativeReply) -> None:
        begun, text, completed = reply.events(
            self._mint_turn_id(), self._session_id, self.generation
        )
        self._totals = self._totals.with_turn(
            completed.usage, measured=not completed.usage_uncertain
        )
        for event in (begun, text, completed):
            self._emit(event)
        before = self._schedules
        self._schedules = before.reply_finished(
            reply.notice.sender, reply.timer_prompt, now=self._clock.now().timestamp()
        )
        if self._schedules is not before:
            self._schedule_changed()

    def _tool_update(self, turn: PromptTurn | None, update: dict[str, object]) -> None:
        for event in self._tools.update(update, turn.ticket.turn_id if turn else None):
            if isinstance(event, ToolUse) and turn is not None:
                self._flush_text(turn)
            self._emit(event)
            self._track_schedule(event)

    def _flush_text(self, turn: PromptTurn) -> None:
        for event in turn.flush_text():
            self._emit(event)
            if isinstance(event, AssistantText):
                self._schedules = self._schedules.on_assistant_text(
                    turn.ticket.turn_id, monotonic_now=self._monotonic()
                )

    def _replay_idle(self) -> None:
        if result := self._subagents.replay_idle(
            self._idle_replies, self._projection, self._session_id
        ):
            projection, replies, needed = result
            for reply in replies:
                self._finish_background_reply(reply)
            self._projection = projection if needed or self._schedules.has_detached else None
            self._background_changed.set()

    def _handle_result(self, request: int, response: dict[str, object]) -> None:
        turn = self._prompt_turn
        if turn is None or request not in turn.requests:
            return
        result = response.get("result")
        reason = result.get("stopReason") if is_object_dict(result) else None
        transcript = self._transcript()
        rows = transcript.rows
        self._ack_inputs(turn, transcript)
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
        if (projection := self._projection) is None:
            self._finish_turn(turn, reason, rows)
        else:
            turn.record_stop(reason, projection)
            turn.stop_deadline = self._monotonic() + self.config.background_reply_seconds
            self._replay(projection, transcript)

    def _ack_inputs(self, turn: PromptTurn, transcript: TranscriptSnapshot) -> None:
        for key in settle_inputs(transcript.rows, transcript.fresh, (*turn.keys, *self._adopted)):
            self._ack_input(turn, key)

    def _ack_input(self, turn: PromptTurn, key: str) -> None:
        if self._ledger.phase(key) is InputPhase.IN_CONTEXT:
            return
        self._apply(self._ledger.in_context(key, turn.ticket.turn_id))
        self._adopted.discard(key)
        self._schedules = self._schedules.on_input(
            turn.ticket.turn_id, monotonic_now=self._monotonic()
        )

    def _finish_turn(
        self,
        turn: PromptTurn,
        reason: str,
        rows: list[dict[str, object]],
    ) -> None:
        ticket = turn.ticket
        self._flush_text(turn)
        self._show_subagents(ticket.turn_id)
        self._apply(self._ledger.lose_unsettled(ticket.turn_id))
        completion = turn.complete(
            rows, reason, self._session_id, self._monotonic(), self.generation
        )
        self._totals = self._totals.with_turn(
            completion.usage, measured=not completion.usage_uncertain
        )
        turn.interrupted = completion.interrupted
        if turn.interrupted:
            for result in self._tools.cancel(ticket.turn_id):
                self._emit(result)
                self._track_schedule(result)
        self._emit(completion)
        if turn.interrupted:
            self._emit(InterruptConfirmed(ticket.turn_id, self.generation))
        self.request_timing_refresh()
        now = self._clock.now().timestamp()
        self._schedules = self._schedules.finish_turn(
            ticket.turn_id, now=now, monotonic_now=self._monotonic(), cancelled=turn.interrupted
        )
        self._schedule_changed()
        self._prompt_turn = None
        self._idle_replies = IdleReplies(len(rows), self.config.max_buffered_events)
        if self._projection is not None:
            turn.close_projection(self._projection, rows)
        elif self._schedules.has_detached:
            self._projection = OutputProjection(len(rows))
        self._complete(ticket)
        turn.finished.set()
        self._recycle_idle_peer()

    def _fail_prompt_turn(self, exc: BaseException) -> None:
        turn, self._prompt_turn = self._prompt_turn, None
        if turn is not None:
            turn.finished.set()
        self._fail_active(exc)
        self._recycle_idle_peer()

    def _recycle_idle_peer(self) -> None:
        transport = self._transport
        if self._recycle_when_idle and self._prompt_turn is None and transport is not None:
            self._retire_transport(transport)

    def _transcript(self, session_id: str | None = None) -> TranscriptSnapshot:
        return TranscriptSnapshot.for_session(session_id or self._session_id, home=self._home)

    async def _watch(self, transport: AgyTransport, generation: int) -> None:
        while self._transport is transport and self._ledger.accepts(generation):
            await self._clock.asleep(TRANSCRIPT_POLL_SECONDS)
            if self._transport is not transport or self._closed:
                return
            transport.observe_children()
            if (error := self._watch_output(transport)) is not None:
                self._retire_transport(transport, RuntimeError(error))
                return
            turn = self._prompt_turn
            if turn is not None:
                self._show_subagents(turn.ticket.turn_id)
                if any(self._ledger.phase(key) is InputPhase.RECEIVED for key in turn.keys):
                    self._ack_inputs(turn, self._transcript())
            if self._adopted and self._transport is transport:
                await self._ack_adopted(transport)

    def _watch_output(self, transport: AgyTransport) -> str | None:
        if not transport.alive:
            return "agy ACP stream ended"
        projection, turn, mono = self._projection, self._prompt_turn, self._monotonic()
        now = self._clock.now().timestamp()
        deadline = self._schedules.reply_deadline(now)
        if turn is None:
            self._replay_idle()
        elif projection is not None and (
            projection.pending or turn.stop_reason or (deadline is not None and deadline <= now)
        ):
            self._replay(projection, self._transcript())
            turn = self._prompt_turn
        if turn is not None and turn.stop_reason and mono >= turn.stop_deadline:
            return "agy ACP output attribution timed out"
        self._schedules, expired = self._schedules.expire_replies(now)
        if expired:
            self._schedule_changed()
            self._recycle_when_idle = True
            logger.warning(
                "Expired agy ACP timer keepalive without a verified reply",
                event="agy_acp_timer_reply_expired",
                timer_count=expired,
                foreground_active=turn is not None,
            )
            self._recycle_idle_peer()
        return None

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
        self._schedules = before.observe_tool(
            event, now=self._clock.now().timestamp(), mono=self._monotonic()
        )
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
        if self._closed or not self._session_id:
            return None
        return t if t is not None and t.alive else None

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
