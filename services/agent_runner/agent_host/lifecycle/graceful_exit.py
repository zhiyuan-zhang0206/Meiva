# meiva:note
# SIGTERM → a bounded drain that leaves every running turn a durable restart: close the
# scheduler's admission, write one `system:restart` inbound per agent with a running turn
# through the ordinary inbound writer, wait for those turns to settle at it, then let the
# ordinary shutdown run. Idle agents get nothing; a turn past the budget is named and
# cancelled, and its restart still wakes it on the next host.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine
from typing import NamedTuple, Protocol

from base.agents.inbox.insert import insert_inbound_message
from base.agents.observation.turn_progress import TurnProgress
from base.daemon.shutdown import sigterm_requests_exit
from base.db import Database
from base.events.live.bus import EventBus
from base.log import logger
from base.native_process.stop_timing import HOST_GRACEFUL_EXIT_DRAIN_S

__all__ = [
    "RESTART_SOURCE",
    "GracefulExit",
    "OwedRestart",
    "TurnTasks",
    "serve_until_exit",
    "write_exit_restart",
]

RESTART_SOURCE = "system:restart"


class TurnTasks(Protocol):
    def task_for(self, agent_id: int) -> asyncio.Task[None] | None: ...

    def stop_admission(self) -> frozenset[int]: ...


# meiva:note
# The lifecycle command that carries one turn's continuation, and whether the exit wrote it.
class OwedRestart(NamedTuple):
    inbound_id: int
    written: bool


# meiva:note
# An agent that already has an unfinished restart or terminate gets no second one: the
# next host would apply both, and a leftover pending restart collides with the maintenance
# hold's own command. The row lock is the one a gateway restart or terminate takes before
# it queues its command.
_OPEN_LIFECYCLE = (
    "SELECT id FROM inbound_messages "
    "WHERE agent_id=%s AND kind IN ('restart','terminate') AND status IN ('pending','claimed') "
    "ORDER BY id LIMIT 1"
)


def _insert_restart(db: Database, bus: EventBus, agent_id: int) -> OwedRestart:
    with db.write_transaction() as conn:
        conn.execute("SELECT 1 FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
        existing = conn.execute(_OPEN_LIFECYCLE, (agent_id,)).fetchone()
        if existing is not None:
            return OwedRestart(existing[0], written=False)
        inbound_id = insert_inbound_message(
            conn, agent_id, "", source=RESTART_SOURCE, kind="restart", database=db, bus=bus
        )
        return OwedRestart(inbound_id, written=True)


async def write_exit_restart(db: Database, bus: EventBus, agent_id: int) -> OwedRestart:
    return await asyncio.to_thread(_insert_restart, db, bus, agent_id)


class GracefulExit:
    def __init__(
        self,
        *,
        turns: TurnTasks,
        progress: TurnProgress,
        write_restart: Callable[[int], Awaitable[OwedRestart]],
        drain_s: float,
    ) -> None:
        self._turns = turns
        self._progress = progress
        self._write_restart = write_restart
        self._drain_s = drain_s
        self._serving: asyncio.Task[object] | None = None
        self._requested = False
        self._owed: frozenset[int] = frozenset()

    @property
    def requested(self) -> bool:
        return self._requested

    # meiva:note
    # Close admission and stop dispatch; `serve` then drains. Idempotent. Closing
    # admission and taking the snapshot is one synchronous step: no turn can start after
    # it, so the snapshot is exactly the turns owed a restart.
    def request(self) -> None:
        if self._requested:
            logger.info("graceful exit already in progress", event="host_exit_repeated")
            return
        self._requested = True
        self._owed = self._turns.stop_admission()
        if self._serving is not None:
            self._serving.cancel()

    # meiva:note
    # Await `dispatch` in this task until it fails or the exit cancels it, then drain. A
    # dispatch failure, and any cancellation besides the exit's own, propagates unchanged.
    async def serve(self, dispatch: Coroutine[object, object, None]) -> None:
        serving = asyncio.current_task()
        if serving is None:
            raise RuntimeError("graceful exit must be served from a task")
        self._serving = serving
        try:
            if self._requested:
                dispatch.close()
            else:
                await dispatch
        except asyncio.CancelledError:
            if not self._requested or serving.uncancel() > 0:
                raise
        finally:
            self._serving = None
        if self._requested:
            await self.drain()

    # meiva:note
    # The writes share the drain's deadline with the wait: a slow database cannot spend the
    # stop ceiling before the turns are even waited for. Every agent left without a restart,
    # by a failed or an unfinished write, is named at ERROR.
    async def drain(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._drain_s
        restarts = await self._write_restarts(deadline)
        tasks = {
            agent_id: task
            for agent_id in sorted(self._owed)
            if (task := self._turns.task_for(agent_id)) is not None
        }
        logger.info(
            "graceful exit: {written} restart(s) written, {reused} unfinished lifecycle "
            "command(s) reused, waiting up to {budget:g}s for {turns} turn(s)",
            event="host_exit_draining",
            written=sum(owed.written for owed in restarts.values()),
            reused=sum(not owed.written for owed in restarts.values()),
            budget=self._drain_s,
            turns=len(tasks),
        )
        pending: set[asyncio.Task[None]] = set()
        if tasks:
            _, pending = await asyncio.wait(
                tasks.values(), timeout=max(0.0, deadline - loop.time())
            )
        unsettled = [agent_id for agent_id, task in tasks.items() if task in pending]
        for agent_id in unsettled:
            owed = restarts.get(agent_id)
            logger.error(
                "graceful exit: the turn of agent {agent_id} did not end within "
                "{budget:g}s (last activity {idle_s}s ago); cancelling it — lifecycle "
                "command {restart} wakes it on the next host",
                event="host_exit_drain_timeout",
                agent_id=agent_id,
                budget=self._drain_s,
                idle_s=_rounded(self._progress.age_s(agent_id)),
                restart=None if owed is None else owed.inbound_id,
            )
        if not unsettled:
            logger.info(
                "graceful exit: every turn it waited for has ended; a restart a turn ended "
                "before reaching stays pending for the next host",
                event="host_exit_drained",
                turns=len(tasks),
            )

    async def _write_restarts(self, deadline: float) -> dict[int, OwedRestart]:
        restarts: dict[int, OwedRestart] = {}
        failures: dict[int, str] = {}
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout_at(deadline):
                for agent_id in sorted(self._owed):
                    try:
                        restarts[agent_id] = await self._write_restart(agent_id)
                    except Exception as exc:
                        failures[agent_id] = repr(exc)
        for agent_id in sorted(self._owed - restarts.keys()):
            logger.error(
                "graceful exit wrote no restart for agent {agent_id} ({reason}); its turn is "
                "still waited for, but if it is cut short nothing wakes it until new input",
                event="host_exit_restart_failed",
                agent_id=agent_id,
                reason=failures.get(agent_id, "the drain deadline passed first"),
            )
        return restarts


def _rounded(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds, 1)


async def serve_until_exit(
    dispatch: Coroutine[object, object, None],
    *,
    turns: TurnTasks,
    progress: TurnProgress,
    write_restart: Callable[[int], Awaitable[OwedRestart]],
) -> None:
    graceful = GracefulExit(
        turns=turns,
        progress=progress,
        write_restart=write_restart,
        drain_s=HOST_GRACEFUL_EXIT_DRAIN_S,
    )
    with sigterm_requests_exit("agent_host", graceful.request):
        await graceful.serve(dispatch)
