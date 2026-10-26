from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass

from base.lm.harness.codex.auth import build_env
from base.lm.harness.codex.config import CodexConfig
from base.lm.harness.codex.mapping import (
    TurnFields,
    approval_denial,
    assistant_text,
    item_event,
    reasoning_delta,
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
from base.lm.harness.codex.types import (
    EVENT_STREAM_END,
    BaseHarnessSession,
    InboundQueueFullError,
    SessionClosedError,
    SessionRegistry,
    SessionTotals,
    TurnOutcome,
    TurnTicket,
)
from base.lm.harness.events import (
    AssistantText,
    SessionFaulted,
    SessionStarted,
    TurnCompleted,
    Usage,
)
from base.lm.harness.handoff import Handoff
from base.lm.harness.process import (
    ChildProbe,
    ProcessObservation,
    describe,
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
_CLIENT_NAME = "meiva-codex-bridge"
_CLIENT_VERSION = "1"
_EVENT_STREAM_END = EVENT_STREAM_END
_INTERRUPT_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "applyPatchApproval",
    }
)


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


class CodexSession(BaseHarnessSession[CodexConfig]):
    def __init__(
        self,
        config: CodexConfig,
        *,
        environ: Mapping[str, str],
        agent_id: int | None = None,
        registry: SessionRegistry | None = None,
        transport: AppServerTransport | None = None,
        probe: ChildProbe | None = None,
    ) -> None:
        super().__init__(
            config,
            agent_id=agent_id,
            registry=registry,
            probe=probe,
            totals=SessionTotals(),
        )
        self._transport: AppServerTransport = transport or StdioAppServer(
            config.command(config.resolved_binary(environ)),
            cwd=config.cwd,
            environ=build_env(environ, api_key_mode=config.api_key_mode),
            request_timeout=config.startup_timeout_seconds,
        )
        self._turn_ids: dict[str, str] = {}
        self._turn_usage: dict[str, Usage] = {}
        self._turn_steps: dict[str, list[Usage]] = {}
        self._final_parts: dict[str, list[str]] = {}
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
        await self._transport.request(
            "initialize",
            {
                "clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION},
                "capabilities": {},
            },
            timeout=self.config.startup_timeout_seconds,
        )
        await self._transport.notify("initialized")
        resolved_resume = self._resolve_resume(resume)
        method, params = self._open_thread_request(resolved_resume)
        result = await self._transport.request(
            method, params, timeout=self.config.startup_timeout_seconds
        )
        self._record_opened_thread(result, resolved_resume)
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
        )
        self._exit_code = self._transport.returncode
        self._finish_close(reason)

    @property
    def cli_pid(self) -> int | None:
        return self._transport.pid

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
            () if gone else describe(observed, [c for c in spawned if self._probe.is_alive(c)])
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

    # meiva:note
    # Build the JSON-RPC method and payload to resume or start the app-server thread.
    def _open_thread_request(self, resume: str | None) -> tuple[str, dict[str, object]]:
        if resume:
            return ("thread/resume", {"threadId": resume})
        params: dict[str, object] = {
            "cwd": str(self.config.cwd),
            "approvalPolicy": self.config.approval_policy,
            "sandbox": self.config.sandbox,
            "config": {"shell_environment_policy": {"inherit": "all"}},
        }
        if self.config.model:
            params["model"] = self.config.model
        return ("thread/start", params)

    # meiva:note
    # Record the opened thread identity and emit `SessionStarted`.
    def _record_opened_thread(self, result: JsonObject, resume: str | None) -> None:
        thread = as_object(result.get("thread"))
        if resume:
            self._session_id = str(thread.get("id") or resume)
            self._resumed_existing = True
        else:
            self._session_id = str(thread.get("id") or "")
        self._model = (
            as_text(result.get("model")) or as_text(thread.get("model")) or self.config.model
        )
        cwd = as_text(result.get("cwd")) or as_text(thread.get("cwd")) or str(self.config.cwd)
        self._emit(
            SessionStarted(
                session_id=self._session_id or "", model=self._model, cwd=cwd, turn_id=None
            )
        )

    async def _begin(self, ticket: TurnTicket) -> None:
        ticket.started_at = self._monotonic()
        self._baseline = snapshot(self._probe, self._transport.pid)
        self._start_deadline(ticket)
        params = {"threadId": self._session_id, "input": [{"type": "text", "text": ticket.text}]}
        try:
            result = await self._transport.request(
                "turn/start", params, timeout=self.config.startup_timeout_seconds
            )
        except Exception as exc:
            self._fail_turn(ticket, exc)
            return
        if codex_turn_id := as_text(as_object(result.get("turn")).get("id")):
            self._bind_codex_turn(ticket, codex_turn_id)

    def _bind_codex_turn(self, ticket: TurnTicket, codex_turn_id: str) -> None:
        if stale := [
            cid
            for cid, tid in self._turn_ids.items()
            if tid == ticket.turn_id and cid != codex_turn_id
        ]:
            for cid in stale:
                self._turn_ids.pop(cid, None)
            self._turn_usage.pop(ticket.turn_id, None)
            self._turn_steps.pop(ticket.turn_id, None)
            self._final_parts.pop(ticket.turn_id, None)
            ticket.parts.clear()
        self._turn_ids[codex_turn_id] = ticket.turn_id

    def _on_notification(self, method: str, params: JsonObject) -> None:
        if method == "item/agentMessage/delta":
            self._on_text_delta(params)
        elif method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
            self._on_reasoning_delta(params)
        elif method in {"item/started", "item/completed"}:
            self._on_item(method, params)
        elif method == "thread/tokenUsage/updated":
            self._on_usage(params)
        elif method == "turn/completed":
            self._handle_turn_completed(params)
        elif method == "error":
            self._on_error(params)

    def _on_text_delta(self, params: JsonObject) -> None:
        if (turn_id := self._turn_id_for(params)) is not None:
            for event in text_delta(params, turn_id):
                self._emit(event)

    def _on_reasoning_delta(self, params: JsonObject) -> None:
        if (turn_id := self._turn_id_for(params)) is not None:
            for event in reasoning_delta(params, turn_id):
                self._emit(event)

    def _on_item(self, method: str, params: JsonObject) -> None:
        if (turn_id := self._turn_id_for(params)) is None:
            return
        item = as_object(params.get("item"))
        if method == "item/completed" and (text := assistant_text(item)) is not None:
            self._record_text(turn_id, item, text)
            self._emit(AssistantText(turn_id=turn_id, text=text))
        phase = "started" if method == "item/started" else "completed"
        for event in item_event(params, phase=phase, turn_id=turn_id):
            self._emit(event)

    def _on_usage(self, params: JsonObject) -> None:
        if (turn_id := self._turn_id_for(params)) is None:
            return
        step = usage_from(params)
        if step.input_tokens == 0 and step.output_tokens == 0:
            return
        self._turn_usage[turn_id] = self._turn_usage.get(turn_id, Usage()) + step
        self._turn_steps.setdefault(turn_id, []).append(step)

    def _on_error(self, params: JsonObject) -> None:
        if not bool(params.get("willRetry", False)):
            turn_id = self._turn_id_for(params)
            error = as_object(params.get("error")).get("message")
            self._emit(
                SessionFaulted(turn_id=turn_id, error=str(error or "app-server reported an error"))
            )

    def _record_text(self, turn_id: str, item: JsonObject, text: str) -> None:
        if (ticket := self._ticket_for(turn_id)) is not None:
            ticket.parts.append(text)
            if assistant_text(item, final_only=True) is not None:
                self._final_parts.setdefault(turn_id, []).append(text)

    def _handle_turn_completed(self, params: JsonObject) -> None:
        turn = as_object(params.get("turn"))
        codex_turn_id = as_text(turn.get("id")) or ""
        ticket = self._active
        bound_codex_id = self._codex_turn_for(ticket.turn_id) if ticket is not None else ""
        turn_id = self._turn_ids.pop(codex_turn_id, None)
        if (
            ticket is None
            or (turn_id is not None and ticket.turn_id != turn_id)
            or (turn_id is None and codex_turn_id and bound_codex_id)
        ):
            self._emit(
                SessionFaulted(
                    turn_id=turn_id,
                    error=f"turn/completed for no active turn ({codex_turn_id or 'unknown'})",
                )
            )
            return
        fields = TurnFields.parse(turn)
        usage = self._turn_usage.pop(ticket.turn_id, Usage())
        steps = tuple(self._turn_steps.pop(ticket.turn_id, ()))
        num_turns = max(len(steps), 1)
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
            num_turns=num_turns,
            steps=steps,
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
                num_turns=num_turns,
                duration_ms=fields.duration_ms,
                usage=usage,
                cost_usd=None,
                cumulative_cost_usd=None,
                permission_denials=tuple(ticket.denials),
                steps=steps,
            )
        )
        self._complete(ticket, outcome)

    def _on_request(self, method: str, params: JsonObject) -> dict[str, object]:
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
        return next((k for k, v in self._turn_ids.items() if v == turn_id), "")

    def _ticket_for(self, turn_id: str) -> TurnTicket | None:
        return (
            self._active if self._active is not None and self._active.turn_id == turn_id else None
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
