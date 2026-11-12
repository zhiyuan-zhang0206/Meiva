"""Upstream chrome-devtools-mcp session lifecycle for the shared browser-MCP daemon.

The daemon owns the Unix-socket multiplexer and the per-connection protocol;
this module owns the one upstream session behind it: creating it (raced
against the stop event and the connect timeout), every teardown await
bounded so a wedged child or SDK cleanup can never hold the daemon past the
operator's stop budget (2026-09-09 #2043), and the stop-aware sleep
(`await_stop_or_timeout`) every loop in this family waits on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import NoReturn

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from base import telemetry
from base.clock import Timers
from base.log import logger
from services.desktop.browser.shutdown_budget import SHUTDOWN_STEP_TIMEOUT_S

# meiva:note
# Pinned exact version — do NOT go back to @latest. npx re-resolves @latest on
# every daemon (re)start, so an upstream release can silently break the browser
# MCP overnight: on 2026-08-02 a newer chrome-devtools-mcp raised its engines
# floor to node ^20.19.0 || ^22.12.0 || >=23 while the session's PATH resolved
# node 18 first, and the upstream refused to start ("chrome upstream session is
# down"). An exact pin keeps deploys reproducible; bump it deliberately with a
# verified node version + a real chrome-MCP smoke test.
_UPSTREAM_PACKAGE = "chrome-devtools-mcp@1.6.0"

# meiva:note
# Same lean flags as the standalone path used: drop the usage-statistics
# telemetry (and its watchdog subprocess) + the periodic update check.
_LEAN_FLAGS = ["--usageStatistics=false"]

# meiva:note
# A wedged (not dead) upstream call must not hold the serial lock forever and
# freeze every client; bound each upstream request. Generous so a slow real
# navigation never trips it -- only a true hang does.
READ_TIMEOUT = timedelta(seconds=180)

# meiva:note
# Every teardown await is bounded by `SHUTDOWN_STEP_TIMEOUT_S` (see
# `services.desktop.browser.shutdown_budget` for the total meiva-root must allow): after
# SIGTERM the daemon must exit even when a cleanup step wedges (a stuck upstream
# stack close, a session loop mid-request). A step that overruns is logged and
# shutdown continues — the process exits anyway, so no live resource is ever
# reported as stopped (2026-09-09 #2043).


class StoppingError(Exception):
    """The stop event fired while the upstream was connecting."""


async def bounded(awaitable: Awaitable[object], what: str) -> None:
    """Await a cleanup step within ``SHUTDOWN_STEP_TIMEOUT_S``.

    A cleanup step that wedges (a stack close waiting on a stuck child, a
    server close racing a half-dead loop) must not hold the daemon past the
    operator's stop budget. On overrun the step is cancelled, the incomplete
    close is logged as such, and shutdown continues.
    """
    try:
        await asyncio.wait_for(awaitable, timeout=SHUTDOWN_STEP_TIMEOUT_S)
    except TimeoutError:
        logger.warning(
            "[browser-mcp] {} did not finish within {:.0f}s; continuing shutdown",
            what,
            SHUTDOWN_STEP_TIMEOUT_S,
        )


async def bounded_stack_close(stack: AsyncExitStack | None, what: str) -> None:
    """Close an upstream stack within the shutdown budget; a failure is logged, not raised."""
    if stack is None:
        return
    try:
        await bounded(stack.aclose(), what)
    except Exception:
        logger.opt(exception=True).warning(
            "[browser-mcp] {} failed; its child processes may linger until the daemon exits",
            what,
        )


# meiva:note
# Sleep for `timeout` seconds on `timers`, waking early if `stop` is set.
async def await_stop_or_timeout(stop: asyncio.Event, timeout: float, timers: Timers) -> None:
    async with asyncio.TaskGroup() as waiters:
        sleeper = waiters.create_task(timers.asleep(timeout))
        waiter = waiters.create_task(stop.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waiter.cancel()


def upstream_server_params(browser_url: str) -> StdioServerParameters:
    return StdioServerParameters(
        command="npx",
        args=[
            "-y",
            _UPSTREAM_PACKAGE,
            "--browserUrl",
            browser_url,
            "--allow-unrestricted-paths",
            *_LEAN_FLAGS,
        ],
        env={**get_default_environment(), "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS": "1"},
    )


async def open_upstream(
    params: StdioServerParameters,
    stop: asyncio.Event,
    *,
    connect_timeout_reader: Callable[[], float],
) -> tuple[ClientSession, AsyncExitStack]:
    stack = AsyncExitStack()
    try:
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(
            ClientSession(read, write, read_timeout_seconds=READ_TIMEOUT.total_seconds())
        )
        await initialize_until_stop_or_timeout(
            session, stop, connect_timeout_reader=connect_timeout_reader
        )
        return session, stack
    except BaseException:
        # meiva:note
        # The failed connect's stack close is bounded too: a stuck child
        # termination must not wedge the reconnect loop (2026-09-09 #2043).
        await bounded_stack_close(stack, "upstream stack close after failed connect")
        raise


async def create_upstream(
    browser_url: str, stop: asyncio.Event, *, connect_timeout_reader: Callable[[], float]
) -> tuple[ClientSession, AsyncExitStack]:
    """Create a new chrome-devtools-mcp upstream session.

    Returns (session, stack) — the caller owns the stack and must close it on
    teardown. The stack owns the subprocess + stdio pipes; when aclosed, the
    npx child is terminated.

    ``session.initialize()`` is raced against both the configured connect
    timeout and the ``stop`` event: SIGTERM during a hung connect interrupts
    the connect immediately instead of waiting out the full connect timeout.
    """
    return await open_upstream(
        upstream_server_params(browser_url),
        stop,
        connect_timeout_reader=connect_timeout_reader,
    )


async def initialize_until_stop_or_timeout(
    session: ClientSession, stop: asyncio.Event, *, connect_timeout_reader: Callable[[], float]
) -> None:
    """Run ``session.initialize()`` until it returns, the stop event fires, or
    the configured connect timeout elapses — whichever comes first.

    SIGTERM during a hung connect wins the race immediately instead of waiting
    out the full connect timeout; a timeout cancels the initialize task so the
    failed connect's stack close can tear the child down cleanly.
    """
    async with asyncio.TaskGroup() as race:
        init_task = race.create_task(_initialize_outcome(session))
        stop_task = race.create_task(stop.wait())
        try:
            await asyncio.wait(
                {init_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=connect_timeout_reader(),
            )
        finally:
            stop_task.cancel()
        if stop.is_set() or not init_task.done():
            init_task.cancel()
    # meiva:note
    # Raised after the group: an exception leaving the body would arrive wrapped.
    if stop.is_set():
        raise StoppingError
    if init_task.cancelled():
        raise TimeoutError("upstream session.initialize() timed out")
    outcome = init_task.result()
    if outcome is not None:
        raise outcome  # the initialize failure


async def _initialize_outcome(session: ClientSession) -> Exception | None:
    """``session.initialize()``'s failure as a value, so it settles the race instead of
    aborting the group that owns the race's tasks."""
    try:
        await session.initialize()
    except Exception as exc:
        return exc
    return None


async def contained(work: Awaitable[object], what: str) -> None:
    """Run one long-lived helper of the daemon so its failure ends it, never the daemon.

    A helper's task lives in a `TaskGroup` whose first failure would abort the daemon;
    this keeps the older meaning of a helper that dies alone — logged, not fatal.
    """
    try:
        await work
    except Exception:
        logger.exception("[browser-mcp] {} failed", what)


# meiva:note
# Upstream session teardown (a closed stdio stream, MCP `CONNECTION_CLOSED`) means the
# upstream process is gone. `REQUEST_TIMEOUT` is NOT in this family: it only says one
# request outlived the read timeout while the session may be perfectly alive (a slow
# navigation, lighthouse, a trace), so it is judged by `UpstreamGuard`, not by its code.
UPSTREAM_DOWN = (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)
UPSTREAM_DOWN_MSG = "chrome upstream session is down; restart browser-mcp before continuing"


# meiva:note
# Why a daemon generation ended (the `reason` of the `browser_mcp_exit` event): `stopped` is
# a service stop or update, not a death; `upstream_exited` the upstream process or its
# connection is gone; `probes_unanswered` the watchdog's pings went unanswered in a row;
# `call_timeout_unanswered` a call timed out and the probe after it was silent;
# `call_timeouts_repeated` calls keep timing out though pings answer.
class DeathReason(StrEnum):
    STOPPED = "stopped"
    UPSTREAM_EXITED = "upstream_exited"
    PROBES_UNANSWERED = "probes_unanswered"
    CALL_TIMEOUT_UNANSWERED = "call_timeout_unanswered"
    CALL_TIMEOUTS_REPEATED = "call_timeouts_repeated"


# meiva:note
# One ping's result: `exited` the connection is closed (nothing can answer any more);
# `unanswered` silence, or an error reply, within the timeout.
class ProbeOutcome(StrEnum):
    ANSWERED = "answered"
    EXITED = "exited"
    UNANSWERED = "unanswered"


# meiva:note
# How the upstream is probed. A wedged upstream has no client calling it to notice, so the
# watchdog pings it every `interval_s` (2026-08-06 #899). One unanswered ping is NOT proof
# of a wedge: `max_unanswered` unanswered probes in a row are, the later ones coming every
# `retry_s` so a real wedge is found quickly. A closed connection needs no repetition and
# is dead at once. Measured 2026-11-12 against chrome-devtools-mcp 1.6.0: its protocol layer
# keeps answering pings during a 40 s navigation, a 40 s lighthouse audit (0.12 s worst
# reply) and a 20 s `wait_for` -- tool calls are async behind a mutex that pings bypass --
# so only a stalled node event loop (a long synchronous burst, a stopped process) leaves
# pings unanswered, and 3 timeouts in a row means 50 s of that.
@dataclass(frozen=True)
class ProbePolicy:
    interval_s: float = 30.0
    timeout_s: float = 10.0
    retry_s: float = 10.0
    max_unanswered: int = 3

    # meiva:note
    # The longest a wedge that starts right after an answered ping stays undetected.
    @property
    def worst_case_s(self) -> float:
        retries = self.max_unanswered - 1
        return self.interval_s + self.timeout_s + retries * (self.retry_s + self.timeout_s)


UPSTREAM_PROBE = ProbePolicy()

# meiva:note
# A call that reaches the read timeout fails alone when the upstream still answers a probe.
# This many such timeouts in a row (no call of any kind succeeding in between) mean the
# upstream's tool path is wedged though its protocol layer answers (a tool stuck while
# holding the upstream's tool mutex), and end the generation. Worst case from the start of
# the first affected call: `CALL_TIMEOUT_LIMIT` read timeouts plus one probe each (380 s).
CALL_TIMEOUT_LIMIT = 2


# meiva:note
# Say why this daemon generation ends, once, so restarts can be counted by cause: a log
# line and the `browser_mcp_exit` event. `STOPPED` is a stop or an update (expected); every
# other reason is a death the root answers with a restart.
def report_daemon_exit(reason: DeathReason) -> None:
    stopped = reason is DeathReason.STOPPED
    logger.log("INFO" if stopped else "ERROR", "[browser-mcp] daemon exiting: {}", reason)
    telemetry.emit(
        "telemetry",
        "browser_mcp_exit",
        level="info" if stopped else "error",
        attributes={"reason": str(reason)},
    )


# meiva:note
# One upstream call outlived the read timeout; the upstream itself still answers.
class UpstreamCallTimeoutError(RuntimeError):
    pass


# meiva:note
# The health verdict on the one upstream session behind a daemon generation: what
# counts as dead, why, and when. `dead` is set once, with the first reason.
class UpstreamGuard:
    def __init__(
        self, session: ClientSession, timers: Timers, policy: ProbePolicy = UPSTREAM_PROBE
    ) -> None:
        self._session = session
        self.timers = timers
        self.policy = policy
        self.dead = asyncio.Event()
        self.reason: DeathReason | None = None
        self._call_timeouts = 0

    def mark_dead(self, reason: DeathReason) -> None:
        if self.dead.is_set():
            return
        self.reason = reason
        self.dead.set()
        logger.error(
            "[browser-mcp] upstream is unusable ({}); ending this daemon generation", reason
        )

    # meiva:note
    # Ping the upstream once, bounded by the policy's timeout.
    async def probe(self) -> ProbeOutcome:
        try:
            await asyncio.wait_for(self._session.send_ping(), timeout=self.policy.timeout_s)
        except TimeoutError:
            return ProbeOutcome.UNANSWERED
        except UPSTREAM_DOWN:
            return ProbeOutcome.EXITED
        except MCPError as e:
            return ProbeOutcome.EXITED if e.code == CONNECTION_CLOSED else ProbeOutcome.UNANSWERED
        except Exception as e:
            logger.warning("[browser-mcp] upstream probe failed unexpectedly: {!r}", e)
            return ProbeOutcome.UNANSWERED
        return ProbeOutcome.ANSWERED

    # meiva:note
    # Await one upstream request. A failure that makes the session unusable marks it dead and
    # raises `RuntimeError(UPSTREAM_DOWN_MSG)`; a read timeout is judged by `_after_timeout`;
    # any other error propagates unchanged.
    async def request[T](self, what: str, work: Awaitable[T]) -> T:
        try:
            result = await work
        except UPSTREAM_DOWN as e:
            self.mark_dead(DeathReason.UPSTREAM_EXITED)
            raise RuntimeError(UPSTREAM_DOWN_MSG) from e
        except MCPError as e:
            if e.code == CONNECTION_CLOSED:
                self.mark_dead(DeathReason.UPSTREAM_EXITED)
                raise RuntimeError(UPSTREAM_DOWN_MSG) from e
            if e.code == REQUEST_TIMEOUT:
                await self._after_timeout(what, e)
            raise
        self._call_timeouts = 0
        return result

    async def _after_timeout(self, what: str, cause: MCPError) -> NoReturn:
        self._call_timeouts += 1
        outcome = await self.probe()
        if outcome is ProbeOutcome.EXITED:
            self.mark_dead(DeathReason.UPSTREAM_EXITED)
        elif outcome is ProbeOutcome.UNANSWERED:
            self.mark_dead(DeathReason.CALL_TIMEOUT_UNANSWERED)
        elif self._call_timeouts >= CALL_TIMEOUT_LIMIT:
            self.mark_dead(DeathReason.CALL_TIMEOUTS_REPEATED)
        if self.dead.is_set():
            raise RuntimeError(UPSTREAM_DOWN_MSG) from cause
        raise UpstreamCallTimeoutError(
            f"{what} did not finish within {READ_TIMEOUT.total_seconds():.0f}s; the browser "
            "itself is still responding, so it was not restarted. The page may be slow or busy: "
            "retry with a lighter operation."
        ) from cause


# meiva:note
# Wait for `dead` or `stop` to fire, cancelling the losing waiter on exit.
async def await_death_or_stop(dead: asyncio.Event, stop: asyncio.Event) -> None:
    async with asyncio.TaskGroup() as waiters:
        dead_task = waiters.create_task(dead.wait())
        stop_task = waiters.create_task(stop.wait())
        try:
            await asyncio.wait({stop_task, dead_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            dead_task.cancel()
            stop_task.cancel()


# meiva:note
# Ping the upstream on the policy's cadence; `max_unanswered` unanswered pings in a row (or a
# closed connection) set `dead` so the daemon exits even when no client is calling.
async def upstream_watchdog(guard: UpstreamGuard, stop: asyncio.Event) -> None:
    unanswered = 0
    while not stop.is_set():
        outcome = await guard.probe()
        if stop.is_set():
            return
        if outcome is ProbeOutcome.EXITED:
            guard.mark_dead(DeathReason.UPSTREAM_EXITED)
            return
        if outcome is ProbeOutcome.ANSWERED:
            unanswered = 0
            wait = guard.policy.interval_s
        else:
            unanswered += 1
            logger.warning(
                "[browser-mcp] upstream ping unanswered ({}/{})",
                unanswered,
                guard.policy.max_unanswered,
            )
            if unanswered >= guard.policy.max_unanswered:
                guard.mark_dead(DeathReason.PROBES_UNANSWERED)
                return
            wait = guard.policy.retry_s
        await await_stop_or_timeout(stop, wait, guard.timers)
