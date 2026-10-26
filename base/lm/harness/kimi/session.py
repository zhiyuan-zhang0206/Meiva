from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from base.lm.harness.acp.transport import (
    AcpClosedError,
    AcpConnection,
    AcpError,
    AcpNotification,
    AcpRpcError,
    AcpTimeoutError,
    PermissionRequest,
)
from base.lm.harness.base_session import (
    EVENT_STREAM_END,
    BaseHarnessSession,
    InboundQueueFullError,
    SessionClosedError,
    SessionRegistry,
)
from base.lm.harness.events import (
    ReasoningDelta,
    SessionFaulted,
    SessionStarted,
    TextDelta,
    TurnCompleted,
    Usage,
)
from base.lm.harness.handoff import Handoff
from base.lm.harness.kimi.config import KimiConfig
from base.lm.harness.kimi.mapping import (
    assistant_text,
    map_update,
    parse_prompt_result,
    reasoning_text,
)
from base.lm.harness.kimi.types import (
    CancelReport,
    SessionTotals,
    TurnOutcome,
    TurnTicket,
)
from base.lm.harness.kimi.usage import UsageSnapshot, read_usage
from base.lm.harness.process import (
    ChildProbe,
    ProcessObservation,
    describe,
    snapshot,
    wait_gone,
)
from base.lm.pricing import CacheTokens, PriceBook, quote

# meiva:note
# One session is one `kimi acp` connection with a single in-flight prompt,
# because ACP refuses concurrent prompts on the same session. Inbound messages
# are queued here and promoted one at a time; a full queue is refused rather
# than buffered, and a repeated key returns the ticket already issued.
#
# Cancellation sends `session/cancel` and sweeps the runtime's descendants to
# reap background commands spawned during the turn. Closing the session watches
# the process tree before shutting down the connection and settles tree exit via
# `settle_close`.
# meiva:link base/lm/harness/kimi/docs/kimi.meiva.okf.md
CANCEL_POLL_SECONDS = 0.05
_EVENT_STREAM_END = EVENT_STREAM_END


def _perf_counter() -> float:
    return time.perf_counter()


class KimiSession(BaseHarnessSession[KimiConfig]):
    def __init__(
        self,
        config: KimiConfig,
        *,
        environ: Mapping[str, str] | None = None,
        agent_id: int | None = None,
        registry: SessionRegistry | None = None,
        probe: ChildProbe | None = None,
        connect: Callable[..., Any] | None = None,
        kill: Callable[[int, int], None] | None = None,
        prices: PriceBook | None = None,
    ) -> None:
        super().__init__(
            config,
            agent_id=agent_id,
            registry=registry,
            probe=probe,
            totals=SessionTotals(),
        )
        self._prices = prices
        self._kill = kill or os.kill
        self._environ = dict(environ) if environ is not None else {}
        self._connect = connect or self.open_connection
        self._connection: AcpConnection | None = None
        self._agent_pid: int | None = None
        self._permission_denials: list[str] = []
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
            {"protocolVersion": 1, "clientCapabilities": {}},
            timeout=self.config.startup_timeout_seconds,
        )
        await self._open_session(connection, self._resolve_resume(resume))
        await self._configure()
        await self._enter_tasks()
        self._reader = self._spawn(self._read_loop(connection))
        self._emit(
            SessionStarted(
                session_id=self._session_id or "",
                model=self.config.model,
                cwd=self.config.workspace,
                turn_id=None,
            )
        )
        self._bind()
        self._inject_handoff(handoff)

    async def _open_session(self, connection: AcpConnection, resume: str | None) -> None:
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
            self._resumed_existing = True
            return
        created = await connection.request(
            "session/new",
            {"cwd": self.config.workspace, "mcpServers": list(self.config.mcp_servers)},
            timeout=self.config.startup_timeout_seconds,
        )
        session_id = created.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpError(f"session/new returned no sessionId: {created!r}")
        self._session_id = session_id

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
        started = _perf_counter()
        if self._session_id is not None:
            await connection.notify("session/cancel", {"sessionId": self._session_id})
        settle_deadline = self._monotonic() + self.config.cancel_settle_seconds
        while ticket is not None and not ticket.settled and self._monotonic() < settle_deadline:
            await asyncio.sleep(CANCEL_POLL_SECONDS)
        cancel_ms = (_perf_counter() - started) * 1000.0
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
        binary = self.config.resolved_binary(self._environ)
        return AcpConnection(
            self.config.command(binary),
            cwd=self.config.workspace,
            environ=self._environment(),
            permission=self.permission_decision,
            request_timeout_seconds=self.config.turn_timeout_seconds,
            shutdown_timeout_seconds=self.config.shutdown_timeout_seconds,
        )

    def _environment(self) -> dict[str, str]:
        env = dict(self._environ)
        if self.config.kimi_home is not None:
            env["KIMI_CODE_HOME"] = str(self.config.kimi_home)
        env.update(self.config.env_overrides)
        return env

    async def permission_decision(self, request: PermissionRequest) -> str | None:
        if self.config.permission_mode == "allow":
            option = request.option("allow_once") or request.option("allow_always")
            return option.option_id if option else None
        denial = request.option("reject_once") or request.option("reject_always")
        self._permission_denials.append(
            str(request.tool_call.get("title") or request.tool_call.get("toolCallId") or "tool")
        )
        return denial.option_id if denial else None

    async def _configure(self) -> None:
        connection = self._require_connection()
        session_id = self._session_id
        if session_id is None:
            return
        if self.config.model is not None:
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
        raw_update: dict[str, Any] | list[Any] | str | int | float | bool | None = params.get(
            "update"
        )
        if not isinstance(raw_update, dict):
            return
        update: dict[str, Any] = raw_update
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
            elif isinstance(event, ReasoningDelta):
                ticket.thoughts.append(event.text)
            self._emit(event)

    async def _begin(self, ticket: TurnTicket) -> None:
        connection = self._require_connection()
        ticket.started_at = self._monotonic()
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
        thought = reasoning_text("\n".join(ticket.thoughts), turn_id=ticket.turn_id)
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
        if thought is not None:
            self._emit(thought)
        assistant = assistant_text(text, turn_id=ticket.turn_id)
        if assistant is not None:
            self._emit(assistant)
        now_mono = self._monotonic()
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
                duration_ms=int((now_mono - (ticket.started_at or now_mono)) * 1000),
                usage=usage,
                cost_usd=cost,
                cumulative_cost_usd=self._totals.cost_usd if self._totals.cost_known else None,
                permission_denials=outcome.permission_denials,
            )
        )
        self._complete(ticket, outcome)

    async def _turn_usage(self) -> tuple[Usage, bool]:
        session_id = self._session_id
        if session_id is None:
            return (Usage(), False)
        kimi_home = self.config.resolved_kimi_home(self._environ)
        observed = await asyncio.to_thread(read_usage, kimi_home, self.config.cwd, session_id)
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
            usage.input_tokens,
            usage.output_tokens,
            CacheTokens(
                read=usage.cache_read_input_tokens,
                write_5m=usage.cache_creation_input_tokens,
            ),
            prices=self._prices,
        )
        return None if quoted is None else quoted.cost_usd

    def _require_connection(self) -> AcpConnection:
        if self._connection is None:
            raise SessionClosedError("session not started")
        return self._connection


def kimi_home_for(workspace: Path) -> Path:
    return Path(workspace) / ".kimi-code"


__all__ = [
    "CANCEL_POLL_SECONDS",
    "EVENT_STREAM_END",
    "CancelReport",
    "InboundQueueFullError",
    "KimiSession",
    "SessionClosedError",
    "SessionTotals",
    "TurnOutcome",
    "TurnTicket",
    "kimi_home_for",
]
