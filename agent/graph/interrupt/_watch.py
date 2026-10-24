# meiva:note
# Interrupt-in-node RAII.
#
# When entering an interruptible section (the LLM stream or code execution), the node uses
# `async with subscribe_interrupt(...) as event:` to get an asyncio.Event; the context manager
# spawns a background task that watches for a durable interrupt inbound (kind 'cancel' or
# 'terminate', or a 'restart' that has waited its grace) for this agent and sets the event the
# moment one is queued. The node races its work against `event.wait()` and aborts when the event
# fires.
#
# Durability is the whole point. The signal is a real `inbound_messages` row (a pending cancel
# inbound or the terminate path), detected via a short DB poll. A signal that lands while no node
# is interruptible is NOT lost: it stays a pending row and the next claim pass dispatches it. This
# node only *aborts* the current action; the semantic dispatch (cancel -> halt to idle, terminate
# -> goto END, restart -> goto END and resume) stays in the claim node, the single owner of
# inbound semantics.
#
# The watcher polls `inbound_messages` on a short cadence (`poll_s`, `INTERRUPT_POLL_S` in
# production) instead of sharing the agent's Redis inbound listener with the claim node's idle
# wait. Sharing was the root cause of a lost-wake class of incidents: the watcher's cancellation
# could be swallowed in the cancel-vs-completion race, leaving an orphan holding the listener's
# `_wait_lock` (and its connection) for up to a full wait cycle while every pub/sub wake for fresh
# inbounds went unheard; the agent then only woke via the claim loop's 30s SELECT recheck, which
# is the user-visible "message stuck ~30s" symptom (2026-08-02, agent 2476, live incident: 30.06s
# pickup). A polling watcher borrows its explicit interrupt pool. Its original service retains a
# cancellation-lost poll until it returns; stop prevents a later poll result from setting the old
# event. A finite caller return does not close its pool.
#
# Cancel latency trades from "instant (pub/sub)" to "<= one poll interval" (2s is well inside
# what a pause/terminate UI action tolerates) and the claim path's instant wake is untouched. A
# restart adds its grace to that (`pending_interrupt_reason`).
#
# Container/eval mode has no inbound queue (pool None); subscribe_interrupt then yields an event
# that never fires, so the wrapped action runs uninterruptibly, as it did before the watcher
# existed.
# meiva:link agent/graph/docs/turn-interrupts.meiva.okf.md

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from agent.db import pending_interrupt_reason
from agent.graph.node_log import awaiter_chain_lines
from base.db.transaction import AsyncConnectionSource
from base.log import logger
from base.native_process.stop_timing import INTERRUPT_POLL_S
from base.native_process.turn_identity import HostedTurnResources

from ._model import InterruptEvent
from ._plan import (
    WATCHER_EXIT_TIMEOUT_S,
    FailureAction,
    Fire,
    WatchStep,
    exit_verdict,
    failure_action,
    watch_step,
    watcher_fate,
)


# meiva:note
# Sets `event` as soon as a pending cancel/terminate row exists for `agent_id`, polling
# `pending_interrupt_reason` every `poll_s`. It deliberately does NOT touch the Redis inbound
# listener, which the claim node's idle wait owns. Between polls it waits on `stop`, so exit is
# prompt. `stop` is the belt to cancellation's braces: a CancelledError can be lost in the
# cancel-vs-completion race, and a watcher that survives its own cancellation would otherwise
# keep polling. Exit sets `stop` before cancelling; the survivor ends at its next loop check, and
# because it holds no shared resource even that lingering poll is harmless.
async def watch_for_interrupt(
    pool: AsyncConnectionSource,
    event: InterruptEvent,
    agent_id: int,
    stop: asyncio.Event,
    *,
    poll_s: float,
) -> None:
    while not stop.is_set():
        step = watch_step(await pending_interrupt_reason(pool, agent_id), stopped=stop.is_set())
        if isinstance(step, Fire):
            event.set(step.reason)
            return
        if step is WatchStep.EXIT:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=poll_s)


# meiva:note
# An invocation's watcher, retained by its original service after a bounded exit.
class _OwnedWatch:
    def __init__(
        self,
        resources: HostedTurnResources,
        agent_id: int,
        event: InterruptEvent,
        stop: asyncio.Event,
        exit_timeout_s: float,
    ) -> None:
        self.resources = resources
        self.agent_id = agent_id
        self.event = event
        self.stop = stop
        self.exit_timeout_s = exit_timeout_s
        self.error: BaseException | None = None
        self.late = False
        self.retained_at = 0.0

    async def run(self, pool: AsyncConnectionSource, poll_s: float) -> None:
        cancelled = False
        try:
            await watch_for_interrupt(pool, self.event, self.agent_id, self.stop, poll_s=poll_s)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except BaseException as error:
            self.error = error
            action = failure_action(late=self.late, stopped=self.stop.is_set())
            if action is FailureAction.REPORT_TO_SERVICE:
                self.resources.require_service().record_failure(
                    self.resources, error, name=f"interrupt-watcher-{self.agent_id}"
                )
            else:
                logger.opt(exception=error).warning(
                    "interrupt watcher failed during invocation (agent_id={})", self.agent_id
                )
                if action is FailureAction.WARN_AND_INTERRUPT:
                    self.event.set()
        finally:
            if self.late:
                logger.info(
                    "retained interrupt watcher finished {elapsed:.1f}s after handoff "
                    "(agent_id={agent_id}): {fate}",
                    elapsed=asyncio.get_running_loop().time() - self.retained_at,
                    agent_id=self.agent_id,
                    fate=watcher_fate(self.error, cancelled=cancelled),
                )

    async def finish(self, task: asyncio.Task[None], primary: BaseException | None) -> None:
        self.stop.set()
        task.cancel()
        loop = asyncio.get_running_loop()
        # meiva:note
        # wait_for would wait out a cancelled cleanup; asyncio.wait keeps the bounded return.
        deadline = loop.time() + self.exit_timeout_s
        cancellation: asyncio.CancelledError | None = None
        while not task.done() and loop.time() < deadline:
            try:
                await asyncio.wait({task}, timeout=max(0, deadline - loop.time()))
            except asyncio.CancelledError as error:
                cancellation = error
        verdict = exit_verdict(
            unwound=task.done(), error=self.error, primary=primary, cancellation=cancellation
        )
        if verdict.retain:
            self.late = True
            self.retained_at = loop.time()
            chain = "\n".join(awaiter_chain_lines(task)) or "  <no frames captured>"
            logger.info(
                "subscribe_interrupt watcher did not unwind {timeout}s after cancel "
                "(agent_id={agent_id}) — retained by its service; wedged await chain:\n{chain}",
                timeout=self.exit_timeout_s,
                agent_id=self.agent_id,
                chain=chain,
            )
        if verdict.raised is None:
            return
        if verdict.note is not None:
            verdict.raised.add_note(verdict.note)
        if verdict.cause is None:
            raise verdict.raised
        raise verdict.raised from verdict.cause


# meiva:note
# RAII watch for a durable interrupt (cancel/terminate) on this agent, owned by the hosted
# service. Yields the event the node races its work against. With `pool` None (container/eval, no
# inbound queue) the event never fires and the wrapped action runs uninterruptibly. On exit
# (completion, exception or cancel) the watcher is stopped and cancelled; if it does not unwind
# within `exit_timeout_s`, its original service keeps the task through stop/join before pool
# close. An unknown in-flight failure reaches the caller; a late failure is reported immediately
# and raised by that service at stop/join, without cancelling sibling agents. `poll_s` and
# `exit_timeout_s` are tuning constants: production keeps their defaults, a test passes small
# values.
# meiva:link agent/graph/docs/turn-interrupts.meiva.okf.md
@asynccontextmanager
async def subscribe_interrupt(
    pool: AsyncConnectionSource | None,
    agent_id: int,
    *,
    resources: HostedTurnResources | None,
    poll_s: float = INTERRUPT_POLL_S,
    exit_timeout_s: float = WATCHER_EXIT_TIMEOUT_S,
) -> AsyncGenerator[InterruptEvent]:
    event = InterruptEvent()
    if pool is None:
        yield event
        return
    if resources is None:
        raise RuntimeError("a database interrupt watcher requires its explicit service resources")
    service = resources.require_service()
    owned = _OwnedWatch(resources, agent_id, event, asyncio.Event(), exit_timeout_s)
    watcher = service.watch(owned.run(pool, poll_s), name=f"interrupt-watcher-{agent_id}")
    primary: BaseException | None = None
    try:
        yield event
    except BaseException as error:
        primary = error
        raise
    finally:
        await owned.finish(watcher, primary)
