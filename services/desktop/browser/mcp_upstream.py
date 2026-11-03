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
from contextlib import AsyncExitStack, suppress
from datetime import timedelta

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

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
_READ_TIMEOUT = timedelta(seconds=180)

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


async def await_stop_or_timeout(stop: asyncio.Event, timeout: float) -> None:
    """Sleep for *timeout* seconds, waking early if *stop* is set."""
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=timeout)


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
            ClientSession(read, write, read_timeout_seconds=_READ_TIMEOUT.total_seconds())
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
# Upstream session teardown or MCP CONNECTION_CLOSED / REQUEST_TIMEOUT ends this daemon generation.
UPSTREAM_DOWN = (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)
_UPSTREAM_DOWN_ERROR_CODES = {CONNECTION_CLOSED, REQUEST_TIMEOUT}
UPSTREAM_DOWN_MSG = "chrome upstream session is down; restart browser-mcp before continuing"

# meiva:note
# Upstream watchdog: with no client calling, a wedged upstream would sit
# undetected forever (ping answers from the daemon, list_tools serves its
# cache). Probe the upstream itself every interval; a missing reply within the
# timeout marks the session dead and ends the process (2026-08-06 #899).
_UPSTREAM_WATCHDOG_INTERVAL_S = 30.0
_UPSTREAM_WATCHDOG_TIMEOUT_S = 10.0


# meiva:note
# True when `exc` means this upstream session is unusable.
def is_upstream_down(exc: BaseException) -> bool:
    return isinstance(exc, UPSTREAM_DOWN) or (
        isinstance(exc, MCPError) and exc.code in _UPSTREAM_DOWN_ERROR_CODES
    )


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
# Periodically ping the upstream session; a wedged one (no reply within the
# timeout) sets `dead` so the daemon exits even when no client is calling.
async def upstream_watchdog(
    upstream: ClientSession, dead: asyncio.Event, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(upstream.send_ping(), timeout=_UPSTREAM_WATCHDOG_TIMEOUT_S)
        except (TimeoutError, MCPError) as e:
            if stop.is_set():
                return
            logger.error(
                "[browser-mcp] upstream watchdog: {} — terminating the daemon",
                type(e).__name__,
            )
            dead.set()
            return
        except Exception as e:
            if stop.is_set():
                return
            logger.warning("[browser-mcp] upstream watchdog ping failed: {!r}; retrying", e)
        await await_stop_or_timeout(stop, _UPSTREAM_WATCHDOG_INTERVAL_S)
