from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

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
    EVENT_STREAM_END,
    BaseHarnessSession,
    InboundQueueFullError,
    SessionClosedError,
    SessionRegistry,
    SessionTotals,
    TurnOutcome,
    TurnTicket,
)
from base.lm.harness.deepseek.usage import UsageSnapshot, read_usage
from base.lm.harness.events import (
    SessionFaulted,
    SessionStarted,
    TextDelta,
    TurnCompleted,
    Usage,
)
from base.lm.harness.handoff import Handoff
from base.lm.harness.process import (
    SIGKILL,
    SIGTERM,
    ChildProbe,
    ProcessObservation,
    PsutilChildProbe,
    describe,
    snapshot,
    wait_gone,
)
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
_EVENT_STREAM_END = EVENT_STREAM_END


def _perf_counter() -> float:
    return time.perf_counter()


# meiva:note
# `CancelReport.settled` is None when the runtime pid is unobservable — an
# unobservable tree is reported as unknown, never as clean — and `remaining` names
# every process that outlived the sweep so the caller decides instead of assuming.
# meiva:link base/lm/harness/deepseek/docs/deepseek.meiva.okf.md
@dataclass(frozen=True, slots=True)
class CancelReport:
    turn_id: str | None
    cancel_ms: float
    settled: bool | None
    observed: tuple[ProcessObservation, ...]
    remaining: tuple[ProcessObservation, ...]
    waited_ms: float
    agent_pid: int | None
    agent_alive: bool | None


# meiva:note
# Summarize the latest turn's token usage from a parsed `UsageSnapshot`.
def _summarize_turn_usage(observed: UsageSnapshot) -> tuple[Usage, bool]:
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


class DeepSeekProbe(ChildProbe, Protocol):
    def kill(self, pid: int, sig: int = 15) -> None: ...


class DeepSeekSession(BaseHarnessSession[BridgeConfig]):
    def __init__(
        self,
        config: BridgeConfig,
        *,
        environ: Mapping[str, str],
        agent_id: int | None = None,
        registry: SessionRegistry | None = None,
        probe: DeepSeekProbe | None = None,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        resolved_probe: DeepSeekProbe = probe if probe is not None else PsutilChildProbe()
        super().__init__(
            config,
            agent_id=agent_id,
            registry=registry,
            probe=resolved_probe,
            totals=SessionTotals(),
        )
        self._deepseek_probe: DeepSeekProbe = resolved_probe
        self._prices: PriceBook | None = config.prices
        self._environ = dict(environ)
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
        resume = self._resolve_resume(resume)
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
        for option_params in self._config_option_requests():
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
                        {"sessionId": self._session_id},
                        timeout=self.config.cancel_settle_seconds,
                    )
            await connection.close(reason=reason)
        await self._close_tasks()
        observed, survivors = self._spawned_descendants()
        if survivors:
            await self._reap(survivors)
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
                self._deepseek_probe.kill(child, SIGTERM)
            reaped.append(child)
        if reaped:
            await asyncio.sleep(CANCEL_POLL_SECONDS)
        for child in reaped:
            if self._probe.is_alive(child):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    self._deepseek_probe.kill(child, SIGKILL)
        return tuple(reaped)

    # meiva:note
    # Snapshot the runtime process tree and select descendants spawned since turn start.
    def _spawned_descendants(self) -> tuple[dict[int, str], dict[int, str]]:
        observed = snapshot(self._probe, self._agent_pid)
        survivors = {
            child: command for child, command in observed.items() if child not in self._baseline
        }
        return (observed, survivors)

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

    def permission_decision(self, request: PermissionRequest) -> str | None:
        mode = self.config.permission_mode
        if mode == "allow":
            option = request.option("allow_once")
            return option.option_id if option else None
        denial = request.option("reject_once")
        self._permission_denials.append(
            str(request.tool_call.get("title") or request.tool_call.get("toolCallId") or "tool")
        )
        return denial.option_id if denial else None

    # meiva:note
    # Build the `session/set_config_option` payloads for configured model and reasoning effort.
    def _config_option_requests(self) -> tuple[dict[str, str], ...]:
        session_id = self._session_id
        if session_id is None or self.config.model is None:
            return ()
        requests = [{"sessionId": session_id, "configId": "model", "value": self.config.model}]
        if self.config.reasoning_effort is not None:
            requests.append(
                {
                    "sessionId": session_id,
                    "configId": "reasoning_effort",
                    "value": self.config.reasoning_effort,
                }
            )
        return tuple(requests)

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
        fields = parse_prompt_result(result)
        text = "\n".join(ticket.parts).strip()
        if observed is None:
            usage, measured = (Usage(), False)
        else:
            self._usage = observed
            usage, measured = _summarize_turn_usage(observed)
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

    def _require_connection(self) -> AcpConnection:
        if self._connection is None:
            raise SessionClosedError("session not started")
        return self._connection


def dsh_home_for(workspace: Path) -> Path:
    return Path(workspace) / ".dsh"


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
    "dsh_home_for",
]
