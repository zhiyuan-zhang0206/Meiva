from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from base.lm.harness.acp import (
    AcpClosedError,
    AcpError,
    AcpNotification,
    AcpRpcError,
    AcpTimeoutError,
)
from base.lm.harness.acp.permission import PermissionRequest
from base.lm.harness.acp.transport import AcpConnection
from base.lm.harness.cancel import (
    CancelReport,
    ProcessObservation,
    Sweep,
    describe,
    observed_cancel,
    spawned_since,
    term_candidates,
    unobserved_cancel,
)
from base.lm.harness.deepseek.config import BridgeConfig
from base.lm.harness.deepseek.environment import environment
from base.lm.harness.deepseek.mapping import map_update
from base.lm.harness.deepseek.requests import (
    INITIALIZE_PARAMS,
    config_option_requests,
    created_session_id,
    permission_choice,
    prompt_params,
    scope_params,
    session_ref,
)
from base.lm.harness.deepseek.settle import (
    ContextOccupancy,
    IgnoredNotification,
    TurnMeasurement,
    TurnSubject,
    TurnUpdate,
    UnclaimedUpdate,
    route_notification,
    settle_turn,
    survivor_fault,
)
from base.lm.harness.deepseek.types import (
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
from base.lm.harness.deepseek.usage import UsageSnapshot
from base.lm.harness.deepseek.usage_files import read_usage
from base.lm.harness.events import SessionFaulted, SessionStarted, TextDelta
from base.lm.harness.handoff import Handoff
from base.lm.harness.process import (
    SIGKILL,
    SIGTERM,
    ChildProbe,
    PsutilChildProbe,
    snapshot,
    wait_gone,
)
from base.lm.pricing import PriceBook

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
_EVENT_STREAM_END = EVENT_STREAM_END


class DeepSeekProbe(ChildProbe, Protocol):
    def kill(self, pid: int, sig: int = 15) -> None: ...


class DeepSeekSession(BaseHarnessSession[BridgeConfig]):
    def __init__(
        self,
        config: BridgeConfig,
        *,
        environ: Mapping[str, str],
        binding: SessionBinding,
        probe: DeepSeekProbe | None = None,
        connect: Callable[..., Any] | None = None,
        clock: SessionClock,
    ) -> None:
        resolved_probe: DeepSeekProbe = probe if probe is not None else PsutilChildProbe()
        super().__init__(
            config,
            binding=binding,
            probe=resolved_probe,
            totals=SessionTotals(),
            clock=clock,
        )
        self._deepseek_probe: DeepSeekProbe = resolved_probe
        self._prices: PriceBook | None = config.prices
        self._environ = dict(environ)
        self._connect = connect or self.open_connection
        self._connection: AcpConnection | None = None
        self._agent_pid: int | None = None
        self._permission_denials: list[str] = []
        self._unclaimed_updates = 0
        self._usage: UsageSnapshot | None = None
        self._context_used: int | None = None
        self._context_size: int | None = None
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
            INITIALIZE_PARAMS,
            timeout=self.config.startup_timeout_seconds,
        )
        resume = self._resolve_resume(resume)
        if resume:
            await connection.request(
                "session/resume",
                scope_params(
                    session_id=resume,
                    workspace=self.config.workspace,
                    mcp_servers=self.config.mcp_servers,
                ),
                timeout=self.config.startup_timeout_seconds,
            )
            self._session_id = resume
        else:
            created = await connection.request(
                "session/new",
                scope_params(workspace=self.config.workspace, mcp_servers=self.config.mcp_servers),
                timeout=self.config.startup_timeout_seconds,
            )
            self._session_id = created_session_id(created)
        for option_params in config_option_requests(
            session_id=self._session_id,
            model=self.config.model,
            reasoning_effort=self.config.reasoning_effort,
        ):
            with contextlib.suppress(AcpRpcError):
                await connection.request(
                    "session/set_config_option",
                    option_params,
                    timeout=self.config.startup_timeout_seconds,
                )
        await self._enter_tasks()
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
                        session_ref(self._session_id),
                        timeout=self.config.cancel_settle_seconds,
                    )
            await connection.close(reason=reason)
        await self._close_tasks()
        observed, survivors = self._spawned_descendants()
        if survivors:
            reaped = self._term_spawned(survivors)
            if reaped:
                await self._clock.asleep(CANCEL_POLL_SECONDS)
                self._kill_survivors(reaped)
        self._residue = self._record_sweep_survivors(observed, survivors, reason="session closed")
        # meiva:note
        # `connection.close()` above waited for the child, so this is final.
        self._exit_code = connection.returncode if connection is not None else None
        self._finish_close(reason)

    @property
    def agent_pid(self) -> int | None:
        return self._agent_pid

    @property
    def usage(self) -> UsageSnapshot | None:
        return self._usage

    @property
    def residue(self) -> tuple[ProcessObservation, ...]:
        return self._residue

    @property
    def context_occupancy(self) -> tuple[int | None, int | None]:
        return (self._context_used, self._context_size)

    # meiva:note
    # Updates the runtime sent while no turn was in flight: dropped at dispatch and
    # counted here, so a reader can tell "the runtime was busy on its own" from "the
    # stream was silent" (#5664).
    # meiva:link base/lm/harness/docs/attribution.meiva.okf.md
    @property
    def unclaimed_updates(self) -> int:
        return self._unclaimed_updates

    async def interrupt(self) -> CancelReport:
        connection = self._require_connection()
        ticket = self._active
        turn_id = None
        if ticket is not None:
            ticket.interrupt_requested = True
            turn_id = ticket.turn_id
        pid = self._agent_pid
        if pid is None:
            return unobserved_cancel(turn_id=turn_id)
        observed = snapshot(self._probe, pid)
        spawned = spawned_since(observed, self._baseline)
        started = self._monotonic()
        if self._session_id is not None:
            await connection.notify("session/cancel", session_ref(self._session_id))
        settle_deadline = self._monotonic() + self.config.cancel_settle_seconds
        while ticket is not None and not ticket.settled and self._monotonic() < settle_deadline:
            await self._clock.asleep(CANCEL_POLL_SECONDS)
        interrupt_ms = (self._monotonic() - started) * 1000.0
        reaped = self._term_spawned(spawned)
        if reaped:
            await self._clock.asleep(CANCEL_POLL_SECONDS)
            self._kill_survivors(reaped)
        gone, waited_ms = await wait_gone(
            self._probe,
            reaped,
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
                alive=[child for child in reaped if self._probe.is_alive(child)],
            ),
        )

    def _term_spawned(self, spawned: Mapping[int, str]) -> tuple[int, ...]:
        alive = {child for child in spawned if self._probe.is_alive(child)}
        reaped = term_candidates(spawned, agent_pid=self._agent_pid, alive=alive)
        for child in reaped:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                self._deepseek_probe.kill(child, SIGTERM)
        return reaped

    def _kill_survivors(self, reaped: tuple[int, ...]) -> None:
        for child in reaped:
            if self._probe.is_alive(child):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    self._deepseek_probe.kill(child, SIGKILL)

    # meiva:note
    # Snapshot the runtime process tree and select descendants spawned since turn start.
    def _spawned_descendants(self) -> tuple[dict[int, str], dict[int, str]]:
        observed = snapshot(self._probe, self._agent_pid)
        return (observed, spawned_since(observed, self._baseline))

    # meiva:note
    # Describe any descendants still alive after `_reap` and emit `SessionFaulted`.
    def _record_sweep_survivors(
        self,
        observed: Mapping[int, str],
        survivors: Mapping[int, str],
        *,
        reason: str,
    ) -> tuple[ProcessObservation, ...]:
        if not survivors:
            return ()
        still = tuple(child for child in survivors if self._probe.is_alive(child))
        if still:
            self._emit(survivor_fault(len(still), reason=reason))
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
        return environment(
            self._environ, dsh_home=self.config.dsh_home, overrides=self.config.env_overrides
        )

    def permission_decision(self, request: PermissionRequest) -> str | None:
        choice = permission_choice(mode=self.config.permission_mode, request=request)
        if choice.denial is not None:
            self._permission_denials.append(choice.denial)
        return choice.option_id

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
        route = route_notification(
            notification, session_id=self._session_id, turn_in_flight=self._active is not None
        )
        match route:
            case IgnoredNotification():
                return
            case ContextOccupancy(used=used, size=size):
                self._context_used = self._context_used if used is None else used
                self._context_size = self._context_size if size is None else size
            case UnclaimedUpdate():
                # meiva:note
                # An update with no turn in flight is the runtime's own work — a background
                # command it started, a sub-session — and `session/update` carries no turn
                # id to attribute it with, so it is dropped and counted here instead of
                # being carried into the next turn (#5664). Mid-turn the two are
                # indistinguishable; that is the adapter's documented residual risk.
                # meiva:link base/lm/harness/docs/attribution.meiva.okf.md
                self._unclaimed_updates += 1
            case TurnUpdate(update=update):
                ticket = self._active
                if ticket is None:
                    # meiva:note
                    # The route was cut from this same read of `_active`; a turn that is gone by
                    # now is the runtime's own work like any other unclaimed update.
                    self._unclaimed_updates += 1
                    return
                for event in map_update(update, turn_id=ticket.turn_id):
                    if isinstance(event, TextDelta):
                        ticket.parts.append(event.text)
                    self._emit(event)

    async def _begin(self, ticket: TurnTicket) -> None:
        connection = self._require_connection()
        ticket.started_at = self._monotonic()
        self._baseline = snapshot(self._probe, self._agent_pid)
        try:
            result = await connection.request(
                "session/prompt",
                prompt_params(session_id=self._session_id, text=ticket.text),
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
        await asyncio.sleep(0)
        observed = (
            await asyncio.to_thread(
                read_usage, self.config.dsh_home, self.config.cwd, self._session_id
            )
            if self._session_id is not None
            else None
        )
        self._settle_turn(ticket, result, observed)

    def _settle_turn(
        self,
        ticket: TurnTicket,
        result: Mapping[str, Any],
        observed: UsageSnapshot | None,
    ) -> None:
        settled = settle_turn(
            TurnSubject(
                turn_id=ticket.turn_id,
                session_id=self._session_id,
                parts=tuple(ticket.parts),
                result=result,
                interrupt_requested=ticket.interrupt_requested,
                permission_denials=tuple(self._permission_denials),
                started_at=ticket.started_at,
            ),
            TurnMeasurement(
                observed=observed,
                model=self.config.model,
                prices=self._prices,
                totals=self._totals,
            ),
            now_monotonic=self._monotonic(),
        )
        if observed is not None:
            self._usage = observed
        self._permission_denials.clear()
        self._totals = settled.totals
        if settled.assistant is not None:
            self._emit(settled.assistant)
        self._emit(settled.completed)
        self._complete(ticket, settled.outcome)

    def _require_connection(self) -> AcpConnection:
        if self._connection is None:
            raise SessionClosedError("session not started")
        return self._connection


__all__ = [
    "CANCEL_POLL_SECONDS",
    "EVENT_STREAM_END",
    "CancelReport",
    "DeepSeekSession",
    "InboundQueueFullError",
    "SessionClosedError",
    "SessionTotals",
    "TurnOutcome",
    "TurnTicket",
]
