from __future__ import annotations

from collections.abc import Mapping

from base.lm.harness.cancel import (
    CancelReport,
    Sweep,
    observed_cancel,
    spawned_since,
    unobserved_cancel,
)
from base.lm.harness.codex.auth import build_env
from base.lm.harness.codex.config import CodexConfig
from base.lm.harness.codex.frames import (
    JsonObject,
    JsonRpcError,
    RequestParams,
    TransportClosedError,
)
from base.lm.harness.codex.mapping import (
    item_event,
    reasoning_delta,
    text_delta,
    usage_from,
)
from base.lm.harness.codex.requests import (
    INITIALIZED,
    OpenedThread,
    answer_request,
    initialize_request,
    interrupt_request,
    open_thread_request,
    turn_start_request,
)
from base.lm.harness.codex.transport import AppServerTransport, StdioAppServer
from base.lm.harness.codex.turns import (
    Inbound,
    NotificationKind,
    TurnRecord,
    binding,
    error_report,
    inbound,
    item,
    settle,
    unclaimed_text,
)
from base.lm.harness.codex.types import (
    EVENT_STREAM_END,
    BaseHarnessSession,
    InboundQueueFullError,
    SessionBinding,
    SessionClock,
    SessionClosedError,
    SessionTotals,
    TurnOutcome,
    TurnTicket,
)
from base.lm.harness.events import (
    AssistantText,
    SessionFailed,
    SessionStarted,
    TurnFaulted,
    UnsolicitedOutput,
)
from base.lm.harness.handoff import Handoff
from base.lm.harness.process import (
    ChildProbe,
    settle_close,
    snapshot,
    wait_gone,
    watch_tree,
)

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


class CodexSession(BaseHarnessSession[CodexConfig]):
    def __init__(
        self,
        config: CodexConfig,
        *,
        environ: Mapping[str, str],
        binding: SessionBinding,
        transport: AppServerTransport | None = None,
        probe: ChildProbe | None = None,
        clock: SessionClock,
    ) -> None:
        super().__init__(
            config,
            binding=binding,
            probe=probe,
            totals=SessionTotals(),
            clock=clock,
        )
        self._transport: AppServerTransport = transport or StdioAppServer(
            config.command(config.resolved_binary(environ)),
            cwd=config.cwd,
            environ=build_env(environ, api_key_mode=config.api_key_mode),
            request_timeout=config.startup_timeout_seconds,
        )
        self._starting = False
        self._records: dict[str, TurnRecord] = {}
        self._started = False

    async def start(self, *, resume: str | None = None, handoff: Handoff | None = None) -> None:
        if self._started:
            raise RuntimeError("session already started")
        if self._closed:
            raise SessionClosedError("session is closed")
        self._transport.on_notification(self._on_notification)
        self._transport.on_request(self._on_request)
        self._transport.on_closed(self._on_transport_closed)
        await self._transport.start()
        await self._enter_tasks()
        self._started = True
        await self._call(*initialize_request())
        await self._transport.notify(INITIALIZED)
        resolved_resume = self._resolve_resume(resume)
        result = await self._call(
            *open_thread_request(
                resolved_resume,
                cwd=str(self.config.cwd),
                model=self.config.model,
                approval_policy=self.config.approval_policy,
                sandbox=self.config.sandbox,
            )
        )
        opened = OpenedThread.of(
            result, resume=resolved_resume, model=self.config.model, cwd=str(self.config.cwd)
        )
        self._session_id = opened.session_id
        self._model = opened.model
        # meiva:note
        # Record the opened thread identity and emit `SessionStarted`.
        self._emit(
            SessionStarted(
                session_id=opened.session_id, model=opened.model, cwd=opened.cwd, turn_id=None
            )
        )
        self._bind()
        self._inject_handoff(handoff)

    async def close(self, *, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        watched = watch_tree(self._probe, self._transport.pid)
        self._fail_pending(SessionClosedError("session closed"))
        await self._close_tasks()
        await self._transport.close()
        await settle_close(
            self._probe,
            watched,
            "codex",
            self.config.cancel_settle_seconds,
            agent_id=self.agent_id,
            clock=self._clock,
        )
        self._exit_code = self._transport.returncode
        self._finish_close(reason)

    @property
    def cli_pid(self) -> int | None:
        return self._transport.pid

    async def interrupt(self) -> CancelReport:
        ticket = self._active
        if ticket is not None:
            self._records[ticket.turn_id] = self._turn_record(ticket.turn_id).interrupted()
        pid = self._transport.pid
        turn_id = ticket.turn_id if ticket is not None else None
        observed = snapshot(self._probe, pid)
        spawned = spawned_since(observed, self._baseline)
        started = self._monotonic()
        if ticket is not None and self._session_id:
            codex_turn_id = ticket.provider_turn_id or ""
            try:
                await self._call(*interrupt_request(self._session_id, codex_turn_id))
            except (JsonRpcError, TransportClosedError, TimeoutError) as exc:
                self._emit(
                    TurnFaulted(turn_id=ticket.turn_id, error=f"turn/interrupt failed: {exc}")
                )
        interrupt_ms = (self._monotonic() - started) * 1000.0
        if pid is None:
            return unobserved_cancel(turn_id=turn_id, interrupt_ms=interrupt_ms)
        gone, waited_ms = await wait_gone(
            self._probe,
            spawned,
            timeout_seconds=self.config.cancel_settle_seconds,
            poll_seconds=CANCEL_POLL_SECONDS,
            clock=self._clock,
        )
        return observed_cancel(
            turn_id=turn_id,
            interrupt_ms=interrupt_ms,
            pid=pid,
            cli_alive=self._probe.is_alive(pid),
            sweep=Sweep(
                observed=observed,
                spawned=spawned,
                gone=gone,
                waited_ms=waited_ms,
                alive=() if gone else [child for child in spawned if self._probe.is_alive(child)],
            ),
        )

    async def _begin(self, ticket: TurnTicket) -> None:
        ticket.started_at = self._monotonic()
        self._baseline = snapshot(self._probe, self._transport.pid)
        self._start_deadline(ticket)
        # meiva:note
        # `turn/started` can beat this response, and the items that follow name the
        # turn by id: the flag is what lets that notification bind the name first, so
        # nothing has to guess whose items arrived early (#5663).
        # meiva:link base/lm/harness/docs/attribution.meiva.okf.md
        self._starting = True
        try:
            result = await self._call(
                *turn_start_request(
                    self._session_id,
                    ticket.text,
                    model=self.config.model,
                    reasoning_effort=self.config.reasoning_effort,
                )
            )
        except Exception as exc:
            self._fail_turn(ticket, exc)
            return
        finally:
            self._starting = False
        self._bind_turn(ticket, result, starting=True)

    def _bind_turn(self, ticket: TurnTicket, announce: RequestParams, *, starting: bool) -> None:
        bound = binding(announce, previous=ticket.provider_turn_id, starting=starting)
        if bound is None:
            return
        if bound.discard:
            # meiva:note
            # A name that changed mid-turn is the provider replaying another turn's
            # transcript: the partial state gathered under the old name is not ours.
            # meiva:link base/lm/harness/docs/attribution.meiva.okf.md
            self._records.pop(ticket.turn_id, None)
        ticket.provider_turn_id = bound.name

    def _on_notification(self, method: str, params: JsonObject) -> None:
        message = inbound(method, params, session_id=self._session_id)
        if message is None:
            return
        match message.kind:
            case NotificationKind.TEXT_DELTA:
                self._on_text_delta(message, params)
            case NotificationKind.REASONING_DELTA:
                self._on_reasoning_delta(message, params)
            case NotificationKind.ITEM_STARTED | NotificationKind.ITEM_COMPLETED:
                self._on_item(message, params)
            case NotificationKind.USAGE:
                self._on_usage(message, params)
            case NotificationKind.TURN_STARTED:
                self._on_turn_started(params)
            case NotificationKind.TURN_COMPLETED:
                self._on_turn_completed(message)
            case NotificationKind.ERROR:
                self._on_error(params)

    def _on_text_delta(self, message: Inbound, params: JsonObject) -> None:
        ticket = self._claim(message.turn_id)
        if ticket is None:
            return
        for event in text_delta(params, turn_id=ticket.turn_id):
            self._emit(event)

    def _on_reasoning_delta(self, message: Inbound, params: JsonObject) -> None:
        ticket = self._claim(message.turn_id)
        if ticket is None:
            return
        for event in reasoning_delta(params, turn_id=ticket.turn_id):
            self._emit(event)

    def _on_item(self, message: Inbound, params: JsonObject) -> None:
        ticket = self._claim(message.turn_id)
        if ticket is None:
            return
        step = item(message.kind, params)
        if step.text is not None:
            record = self._turn_record(ticket.turn_id).with_text(step.text, final=step.final)
            self._records[ticket.turn_id] = record
            self._emit(AssistantText(turn_id=ticket.turn_id, text=step.text))
        for event in item_event(params, phase=step.phase, turn_id=ticket.turn_id):
            self._emit(event)

    def _on_usage(self, message: Inbound, params: JsonObject) -> None:
        ticket = self._claim(message.turn_id)
        if ticket is None:
            return
        record = self._turn_record(ticket.turn_id).with_usage(usage_from(params))
        self._records[ticket.turn_id] = record

    # meiva:note
    # An error the server will not retry, on a turn this session is not running, is
    # news about the provider rather than a verdict on our turn. It reaches the sink
    # instead of ending a wait it has nothing to do with (#5663).
    # meiva:link base/lm/harness/docs/attribution.meiva.okf.md
    def _on_error(self, params: JsonObject) -> None:
        report = error_report(params)
        if report is None:
            return
        ticket = self._claim(report.turn_id)
        if ticket is None:
            self._unsolicited(UnsolicitedOutput(source="error", text=report.text))
            return
        self._emit(TurnFaulted(turn_id=ticket.turn_id, error=report.text))

    # meiva:note
    # `turn/started` names the turn the server is about to run, and it can arrive
    # before `turn/start` has answered. Binding that name here is what lets the items
    # that follow be attributed without guessing: only a turn this session is in the
    # middle of starting may take a name, so a name announced at any other moment
    # belongs to a turn we did not start (#5663).
    # meiva:link base/lm/harness/docs/attribution.meiva.okf.md
    def _on_turn_started(self, params: JsonObject) -> None:
        ticket = self._active
        if ticket is None:
            return
        self._bind_turn(ticket, params, starting=self._starting)

    def _on_turn_completed(self, message: Inbound) -> None:
        ticket = self._claim(message.turn_id)
        if ticket is None:
            # meiva:note
            # A completion for a turn this session did not run: the server's own work
            # (a queued, reviewed or compacted turn), or a turn an earlier session ran
            # and the served transcript replays. It is neither our boundary nor a
            # failure, and its reply is content the agent has not read — so it goes to
            # the sink the way any other inbound does (#5663).
            # meiva:link base/lm/harness/docs/attribution.meiva.okf.md
            self._unsolicited(
                UnsolicitedOutput(source="turn/completed", text=unclaimed_text(message.turn))
            )
            return
        settlement = settle(
            turn_id=ticket.turn_id,
            session_id=self._session_id,
            turn=message.turn,
            record=self._turn_record(ticket.turn_id),
            totals=self._totals,
        )
        self._records.pop(ticket.turn_id, None)
        self._totals = settlement.totals
        self._emit(settlement.completed)
        self._complete(ticket, settlement.outcome)

    def _on_request(self, method: str, params: JsonObject) -> JsonObject:
        answer = answer_request(method, params, decision=self.config.approval_decision)
        ticket = self._active
        if answer.denial is not None and ticket is not None:
            record = self._turn_record(ticket.turn_id).with_denial(answer.denial)
            self._records[ticket.turn_id] = record
        return answer.answer

    def _on_transport_closed(self, reason: str) -> None:
        if self._closed:
            return
        self._emit(SessionFailed(error=f"app-server: {reason}"))
        self._fail_active(TransportClosedError(reason))

    def _turn_record(self, turn_id: str) -> TurnRecord:
        return self._records.get(turn_id) or TurnRecord()

    async def _call(self, method: str, params: RequestParams) -> JsonObject:
        return await self._transport.request(
            method, params, timeout=self.config.startup_timeout_seconds
        )


__all__ = [
    "CANCEL_POLL_SECONDS",
    "EVENT_STREAM_END",
    "CancelReport",
    "CodexSession",
    "InboundQueueFullError",
    "SessionClosedError",
    "SessionTotals",
    "TurnOutcome",
    "TurnTicket",
]
