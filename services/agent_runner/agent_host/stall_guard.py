"""Turn-level no-progress stall guard — one `graph.ainvoke` under a timeout.

The hosted runner's turn-level injection guard (task #2417 half 2): a graph
invocation whose turn-progress clock has shown no activity for
``MEIVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS`` is aborted instead of hanging
forever under a live heartbeat lease — agent 2998's exact failure (claimed its
whole inbound queue, then hung inside ``graph.ainvoke`` for 3.5 hours with the
lease renewing).

Engineered as module-level functions rather than `AgentHost` methods to keep
``host.py`` inside the 800-line ceiling; also the honest shape — the guard is a
pure input/output function of (graph, agent_id, ctx, config, input) and keeps
no host state.

The guard, not a wall clock, is the timeout. A turn that keeps making progress
may legitimately run for days (the hosted design explicitly allows long
autonomous loops), so an absolute turn budget would be wrong; only silence
counts. The clock (agent/turn/progress.py) is marked by every LangGraph node
enter, completed LLM step and streamed LLM chunk, so a long exec or a long
model stream keeps the turn alive while a graph-level hang — or a provider
hanging without ever completing a step — does not.
"""

from __future__ import annotations

import asyncio

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from agent.state import BaseAgentState
from agent.turn.runloop import emit_error_event, is_settled_invocation_abort
from base.agents.context import MeivaContext
from base.log import logger
from base.native_process.turn_identity import HostedServiceResources, HostedTurnResources
from services.agent_runner.agent_host.runtime import StallTiming

_Graph = CompiledStateGraph[BaseAgentState, MeivaContext, BaseAgentState, BaseAgentState]


class HostRestartRequiredError(RuntimeError):
    """A hosted turn refused its bounded cancellation and owns the task slot.

    Rescheduling beside it would violate one-turn-per-agent. Letting the daemon
    exit hands recovery to its supervisor, which restarts the host. A later fresh input may start new work.
    """


class TurnStallTimeoutError(RuntimeError):
    """A hosted turn was aborted by the no-progress stall guard (host.py).

    Its ``graph.ainvoke`` made no progress for
    ``MEIVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS`` and was cancelled; the turn
    task ends (the task DID unwind — that is the whole point of the bounded
    abort) and a later fresh input starts a new work. Distinct from
    ``HostRestartRequiredError``, which is raised when the cancel refuses to
    unwind: the daemon there must exit, here it must not.
    """


# meiva:note
# Retrieve a finished foreground invocation's result, recording only unsettled errors into
# `HostedServiceResources` so handled turn aborts do not fail daemon shutdown.
# meiva:link services/docs/agent_runner_side/agent_runner_side.meiva.okf.md
def _unwrap_invocation_result(
    service: HostedServiceResources,
    scope: HostedTurnResources,
    invoke_task: asyncio.Task[dict[str, object]],
) -> dict[str, object]:
    try:
        return invoke_task.result()
    except asyncio.CancelledError:
        raise
    except BaseException as error:
        if not is_settled_invocation_abort(error):
            service.record_failure(scope, error, name=invoke_task.get_name())
        raise


# meiva:note
# Run one ``graph.ainvoke`` under the turn-level no-progress stall guard.
#
# On stall: the invocation task is cancelled inside the host's bounded unwind
# window (`timing.unwind_timeout_s`). Unwound → `TurnStallTimeoutError` (the
# turn task ends; ``run_turn`` drops the runtime, so the next wake re-runs
# startup reconciliation and waits for new input). Refused →
# `HostRestartRequiredError` (the daemon must exit; the supervisor restarts
# it — rescheduling beside a task that still owns the slot would violate
# one-turn-per-agent).
#
# An EXTERNAL cancellation (the dispatcher's stale-turn scan, force
# terminate, shutdown) lands on THIS task's awaits rather than the graph's,
# because the graph now runs one level down. It is mirrored onto the graph
# task and its unwind is awaited here, so the external cancellation keeps
# exactly the unwinding semantics it had before this guard existed — when
# the graph task WAS the turn task.
async def run_invocation_with_stall_guard(
    graph: _Graph,
    agent_id: int,
    ctx: MeivaContext,
    config: RunnableConfig,
    input_update: dict[str, object],
    *,
    timing: StallTiming,
) -> dict[str, object]:
    ctx.turn_progress.reset(agent_id)
    scope = ctx.hosted_resources
    if scope is None:
        raise RuntimeError("a hosted invocation requires its original resource scope")
    service = scope.require_service()
    invoke_task = asyncio.create_task(
        graph.ainvoke(  # pyright: ignore[reportUnknownMemberType, reportAssignmentType]
            input_update,  # pyright: ignore[reportArgumentType, reportUnknownMemberType]
            config=config,
            context=ctx,
        ),
        name=f"turn-ainvoke-{agent_id}",
    )
    service.hold_task(scope, invoke_task)
    try:
        while True:
            done, _pending = await asyncio.wait(
                {invoke_task},
                timeout=timing.scan_interval_s,
            )
            if invoke_task in done:
                return _unwrap_invocation_result(service, scope, invoke_task)
            age = ctx.turn_progress.age_s(agent_id)
            if age is not None and age >= timing.no_progress_timeout_s:
                return await _abort_stalled_invocation(
                    agent_id,
                    ctx,
                    service,
                    scope,
                    invoke_task,
                    age,
                    unwind_timeout_s=timing.unwind_timeout_s,
                )
    except asyncio.CancelledError:
        if not invoke_task.done():
            invoke_task.cancel()
            done, _ = await asyncio.wait({invoke_task}, timeout=timing.unwind_timeout_s)
            if invoke_task not in done:
                service.retain_task(scope, invoke_task)
                raise HostRestartRequiredError("graph did not unwind after cancellation") from None
        if not invoke_task.cancelled():
            _unwrap_invocation_result(service, scope, invoke_task)
        raise


# meiva:note
# Cancel a stalled invocation, emit one best-effort SSE `Error` event so the frontend shows the
# abort instead of silence, and classify the unwind using `asyncio.wait` (never
# `asyncio.wait_for(asyncio.shield(...))`, whose outer shield hangs when the inner task cancels):
# returns the result if the invocation finished before the cancel landed, raises
# `TurnStallTimeoutError` when it unwound cancelled, and retains the straggler via `retain_task`
# before raising `HostRestartRequiredError` when it refused to unwind.
async def _abort_stalled_invocation(
    agent_id: int,
    ctx: MeivaContext,
    service: HostedServiceResources,
    scope: HostedTurnResources,
    invoke_task: asyncio.Task[dict[str, object]],
    age_s: float,
    *,
    unwind_timeout_s: float,
) -> dict[str, object]:
    logger.error(
        "hosted turn for agent {agent_id} made no progress for "
        "{no_progress_s:.0f}s — aborting the invocation; the next wake "
        "waits for fresh input",
        event="host_turn_stall_timeout",
        agent_id=agent_id,
        no_progress_s=age_s,
    )
    emit_error_event(
        ctx,
        agent_id,
        f"The turn was aborted after {age_s:.0f}s with no progress. "
        "Send a new message to start new work.",
    )
    invoke_task.cancel()
    done, _pending = await asyncio.wait({invoke_task}, timeout=unwind_timeout_s)
    if invoke_task not in done:
        service.retain_task(scope, invoke_task)
        logger.error(
            "hosted turn for agent {agent_id} did not unwind within the "
            "bounded window after a stall-cancel — the daemon must exit; "
            "the supervisor restarts it",
            event="host_turn_stall_uncancellable",
            agent_id=agent_id,
        )
        raise HostRestartRequiredError(
            f"hosted turn for agent {agent_id} did not unwind after a stall-cancel"
        ) from None
    try:
        return _unwrap_invocation_result(service, scope, invoke_task)
    except asyncio.CancelledError:
        raise TurnStallTimeoutError(agent_id) from None
