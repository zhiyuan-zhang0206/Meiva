from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from base.host.env.harness_binaries import resolve_harness_binary
from base.lm.harness.base_session import BaseHarnessSession, SessionBinding, TurnTicket
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
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
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
    steer_request,
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
from base.lm.harness.events import (
    AssistantText,
    ForegroundAccepted,
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
from base.lm.harness.session import (
    EVENT_STREAM_END,
    InboundQueueFullError,
    SessionClock,
    SessionClosedError,
    SessionTotals,
    TurnOutcome,
)

# meiva:note
# The three boundaries are the ones every harness bridge has, expressed in
# app-server terms. Ordinary inbound waits for `turn/completed`. Foreground
# input uses `turn/steer` with an active-turn precondition. One outbound queue
# preserves control events and may drop text deltas. Cancellation is `turn/interrupt` plus an
# assertion that the commands the turn spawned are gone, and `settled=None` when
# the app-server pid is unobservable — never a silent pass.
# meiva:link base/lm/harness/codex/docs/codex.meiva.okf.md
CANCEL_POLL_SECONDS = 0.02


# meiva:note
# The CLI this session spawns, resolved through the directories a harness installs into as well as
# PATH: the session environment it is handed need not carry a PATH at all, and a bare name would
# then resolve against the OS default (#5491).
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def resolved_binary(
    config: CodexConfig, environ: Mapping[str, str], *, bin_dirs: Sequence[str] | None = None
) -> str:
    return resolve_harness_binary(config.binary, path_env=environ.get("PATH"), bin_dirs=bin_dirs)


async def list_models(
    binary: str, *, cwd: Path, environ: Mapping[str, str], timeout: float
) -> JsonObject:
    config = CodexConfig(cwd=cwd)
    server = StdioAppServer(
        config.command(binary),
        cwd=config.cwd,
        environ=build_env(environ),
        stderr=subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(timeout):
            await server.start()
            _ = await server.request(*initialize_request())
            await server.notify(INITIALIZED)
            return await server.request("model/list", {})
    finally:
        await server.close()


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
            config.command(resolved_binary(config, environ)),
            cwd=config.cwd,
            environ=build_env(environ, api_key_mode=config.api_key_mode),
            request_timeout=config.startup_timeout_seconds,
        )
        self._starting = False
        self._early_notifications: list[tuple[Inbound, JsonObject]] = []
        self._records: dict[str, TurnRecord] = {}
        self._steer_receipts: dict[tuple[str, str], asyncio.Future[bool]] = {}
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
        self._finish_steer_receipts()
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

    async def interrupt(self, *, turn_id: str | None = None) -> CancelReport:
        ticket = await self._interrupt_target(turn_id)
        if ticket is None or ticket.provider_turn_id is None or self._session_id is None:
            return unobserved_cancel(turn_id=turn_id)
        pid = self._transport.pid
        turn_id = ticket.turn_id
        observed = snapshot(self._probe, pid)
        spawned = spawned_since(observed, self._baseline)
        started = self._monotonic()
        try:
            await self._call(*interrupt_request(self._session_id, ticket.provider_turn_id))
        except Exception:
            ticket.interrupt_requested = False
            raise
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

    def _steer_target(self) -> tuple[str, str] | None:
        ticket = self._active
        thread_id = self._session_id
        if (
            self._closed
            or self._starting
            or ticket is None
            or ticket.interrupt_requested
            or not thread_id
            or not ticket.provider_turn_id
        ):
            return None
        return thread_id, ticket.provider_turn_id

    @property
    def steering_available(self) -> bool:
        return self._steer_target() is not None

    async def steer(self, text: str, *, key: str) -> bool:
        if not text.strip() or not key:
            raise ValueError("steering requires nonempty text and a message key")
        target = self._steer_target()
        if target is None:
            return False
        thread_id, turn_id = target
        identity = (turn_id, key)
        if identity in self._steer_receipts:
            raise RuntimeError("steer request already pending for this message key")
        receipt: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._steer_receipts[identity] = receipt
        try:
            try:
                response = await self._call(*steer_request(thread_id, turn_id, text, key=key))
            except JsonRpcError as exc:
                if exc.code in (INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS):
                    return False
                raise
            if response.get("turnId") != turn_id:
                raise RuntimeError("Codex steer response did not name the expected turn")
            if not await receipt:
                raise RuntimeError("Codex turn ended before the steer receipt arrived")
            return True
        finally:
            del self._steer_receipts[identity]

    def _finish_steer_receipts(self, *, turn_id: str | None = None) -> None:
        for (pending_turn, _), receipt in self._steer_receipts.items():
            if (turn_id is None or pending_turn == turn_id) and not receipt.done():
                receipt.set_result(False)

    async def _begin(self, ticket: TurnTicket) -> None:
        ticket.started_at = self._monotonic()
        self._baseline = snapshot(self._probe, self._transport.pid)
        # meiva:note
        # `turn/started` can beat this response and supply the interrupt target.
        # Activity waits for the response to certify its native turn id.
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
            early, self._early_notifications = self._early_notifications, []
        if self._active is not ticket:
            return
        if not self._bind_turn(ticket, result, starting=True):
            self._fail_turn(ticket, RuntimeError("Codex turn/start response did not name a turn"))
            return
        self._emit(ForegroundAccepted(turn_id=ticket.turn_id))
        for message, params in early:
            self._dispatch_notification(message, params)

    def _bind_turn(self, ticket: TurnTicket, announce: RequestParams, *, starting: bool) -> bool:
        bound = binding(announce, previous=ticket.provider_turn_id, starting=starting)
        if bound is None:
            return False
        if bound.discard:
            # meiva:note
            # A name that changed mid-turn is the provider replaying another turn's
            # transcript: the partial state gathered under the old name is not ours.
            # meiva:link base/lm/harness/docs/attribution.meiva.okf.md
            self._records.pop(ticket.turn_id, None)
        ticket.provider_turn_id = bound.name
        ticket.interrupt_ready.set()
        return True

    def _on_notification(self, method: str, params: JsonObject) -> None:
        message = inbound(method, params, session_id=self._session_id)
        if message is None:
            return
        if self._starting and message.kind is not NotificationKind.TURN_STARTED:
            self._buffer_early_notification(message, params)
            return
        self._dispatch_notification(message, params)

    def _buffer_early_notification(self, message: Inbound, params: JsonObject) -> None:
        if not message.turn_id or params.get("threadId") != self._session_id:
            return
        if (
            message.kind in (NotificationKind.TEXT_DELTA, NotificationKind.REASONING_DELTA)
            and len(self._early_notifications) >= self.config.max_buffered_events
        ):
            self._dropped_deltas += 1
            return
        self._early_notifications.append((message, params))

    def _dispatch_notification(self, message: Inbound, params: JsonObject) -> None:
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
        if (
            step.user_message_key is not None
            and message.turn_id is not None
            and params.get("threadId") == self._session_id
        ):
            receipt = self._steer_receipts.get((message.turn_id, step.user_message_key))
            if receipt is not None and not receipt.done():
                receipt.set_result(True)
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
    # before `turn/start` has answered. Only a turn this session is in the middle
    # of starting may provisionally take the name so interruption can target it.
    # meiva:link base/lm/harness/docs/attribution.meiva.okf.md
    def _on_turn_started(self, params: JsonObject) -> None:
        ticket = self._active
        if ticket is None:
            return
        _ = self._bind_turn(ticket, params, starting=self._starting)

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
        if message.turn_id:
            self._finish_steer_receipts(turn_id=message.turn_id)
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
        self._finish_steer_receipts()
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
