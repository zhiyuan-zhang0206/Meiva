from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast

from base.host.env.harness_binaries import resolve_harness_binary
from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.mapping import (
    assistant_text,
    map_init,
    map_step_update,
    parse_result,
    parse_usage,
)
from base.lm.harness.antigravity.transport import (
    AgyTransport,
    InterruptReport,
    TransportClosedError,
)
from base.lm.harness.base_session import (
    EVENT_STREAM_END,
    BaseHarnessSession,
    InboundQueueFullError,
    SessionClosedError,
    SessionRegistry,
    SessionTotals,
    TurnOutcome,
    TurnTicket,
    add_cost,
)
from base.lm.harness.events import (
    SessionFaulted,
    TurnCompleted,
    Usage,
)
from base.lm.harness.handoff import Handoff
from base.lm.harness.process import (
    ChildProbe,
    settle_close,
    snapshot,
    watch_tree,
)

# meiva:note
# Same three boundaries as the Claude bridge — ordered inbound, one outbound
# stream, an assertable cancel — with one harness difference that shapes the
# whole file: an interrupt kills the `agy` process. `interrupt()` therefore
# retires the transport, and the next turn respawns it with `--conversation`, so
# the transcript survives even though the process does not.
# meiva:link base/lm/harness/antigravity/docs/antigravity.meiva.okf.md
CancelReport = InterruptReport
_add_cost = add_cost


# meiva:note
# One `agy` conversation as a turn engine.
class AgySession(BaseHarnessSession[AntigravityConfig]):
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
        super().__init__(
            config,
            agent_id=agent_id,
            registry=registry,
            probe=probe,
            totals=SessionTotals(cost_usd=None),
        )
        self._environ = dict(environ) if environ else None
        self._factory = transport_factory or self._default_transport
        self._transport: AgyTransport | None = None
        self._retired: AgyTransport | None = None
        self._step_text: dict[int, list[str]] = {}
        self._turn_steps: list[Usage] = []

    async def start(self, *, resume: str | None = None, handoff: Handoff | None = None) -> None:
        if self._transport is not None:
            raise RuntimeError("session already started")
        if self._closed:
            raise SessionClosedError("session is closed")
        resume = self._resolve_resume(resume)
        transport = self._factory()
        await transport.start(resume=resume)
        self._transport = transport
        await self._enter_tasks()
        self._reader = self._spawn(self._read_loop(transport))
        self._inject_handoff(handoff)

    async def close(self, *, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        self._fail_pending(SessionClosedError("session closed"))
        await self._close_tasks()
        transport, self._transport = self._transport, None
        watched = watch_tree(self._probe, transport.pid if transport is not None else None)
        if transport is not None:
            await transport.close()
        await settle_close(
            self._probe,
            watched,
            "antigravity",
            self.config.cancel_settle_seconds,
            agent_id=self.agent_id,
        )
        # meiva:note
        # After the child was reaped: `settle_close` above waited for its tree, and
        # `transport.close()` waited for the process itself, so this is final.
        self._exit_code = transport.returncode if transport is not None else None
        self._finish_close(reason)

    @property
    def cli_pid(self) -> int | None:
        return self._transport.pid if self._transport is not None else None

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
            # meiva:note
            # Nothing is running: keep the healthy process instead of killing
            # it.
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
        # meiva:note
        # Retire before signalling: the reader may see EOF while the report is
        # still being assembled, and an expected exit must not look like a
        # crash.
        self._retired = transport
        return await transport.interrupt(turn_id=ticket.turn_id, baseline=self._baseline)

    def _default_transport(self) -> AgyTransport:
        # meiva:note
        # A bare `agy` resolves against the child's PATH, and a harness child is handed an
        # environment with none (#5491). Pin the CLI we resolved; the rest of the argv
        # prefix (a test's fake CLI, an operator's flags) rides unchanged.
        # meiva:link base/lm/harness/docs/harness.meiva.okf.md
        resolved = resolve_harness_binary(
            self.config.executable[0], path_env=(self._environ or {}).get("PATH")
        )
        return AgyTransport(
            replace(self.config, executable=(resolved, *self.config.executable[1:])),
            environ=self._environ,
            probe=self._probe,
        )

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

    async def _begin(self, ticket: TurnTicket) -> None:
        ticket.started_at = self._monotonic()
        self._step_text = {}
        self._turn_steps = []
        try:
            transport = await self._ensure_transport()
            self._baseline = snapshot(self._probe, transport.pid)
            self._start_deadline(ticket)
            await transport.send(ticket.text)
        except (TransportClosedError, OSError, RuntimeError) as exc:
            self._fail_turn(ticket, exc)

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
            # meiva:note
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
            self._model = event.model or self._model
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
                if update.get("step_type") == "agent_response" and isinstance(
                    update.get("usage"), dict
                ):
                    self._turn_steps.append(parse_usage(update.get("usage")))
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
        steps = tuple(self._turn_steps)
        num_turns = max(len(steps), fields.num_turns, 1)
        self._emit(
            TurnCompleted(
                turn_id=ticket.turn_id,
                session_id=fields.session_id or self._session_id,
                text=text,
                subtype=fields.subtype,
                is_error=fields.is_error,
                interrupted=ticket.interrupt_requested or fields.interrupted,
                stop_reason=fields.status,
                num_turns=num_turns,
                duration_ms=fields.duration_ms,
                usage=fields.usage,
                cost_usd=None,
                cumulative_cost_usd=self._totals.cost_usd,
                permission_denials=fields.denied_actions,
                steps=steps,
            )
        )
        self._complete(ticket, outcome)


__all__ = [
    "EVENT_STREAM_END",
    "AgySession",
    "CancelReport",
    "InboundQueueFullError",
    "SessionClosedError",
    "SessionTotals",
    "TurnOutcome",
    "TurnTicket",
    "_add_cost",
]
