"""claim node: long await + dispatch by inbound kind.

Replaces V1's transitional entry_node + loop.py's pick_thread/claim/mark_done
flow. Per framework-rearchitecture v2 unified gateway design: all agent
control signals pass through the inbound table, dispatched here by kind (see
docs/decisions/agents/graph/2026-05-02-self-cycling-langgraph.md +
docs/decisions/agents/messages/2026-04-26-inbound-queue.md).

This module is the pipeline orchestrator; the per-axis logic lives in
co-located modules (Task #1006 split — the original 979-line file was divided
by the batch-claim / kind-dispatch / lifecycle-routing axes, behavior preserved):

- `_batch.py`     — batch acquisition: idle wait loop, batch claim, idle trim, chat deferral
- `_routing.py`   — lifecycle routing: ClaimGoto vocabulary + batch winner resolution
- `_dispatch.py`  — per-kind dispatch: batch state, lifecycle markers, handlers, dispatch loop
- `_decide.py`    — decision: post-dispatch short-circuit rules → single Command
- `_present.py`   — display: SSE publishing for the frontend timeline

Pipeline (see claim_node_impl): container early-return → first SELECT →
idle wait (if halted / no conversation) → routing → dispatch → decide → END
snapshot. The full dispatch-by-kind behavior spec and the routing winner
semantics are preserved in the submodule docstrings above.

after_exec **always** routes to claim now (no longer only when halted=True),
ensuring user chat in the middle of a multi-step loop can also be promptly
claimed and merged into the next LLM round.

Automatic compaction runs in the LLM node via `agent/graph/hooks/compact.py:auto_compact_for_llm`
after the before_llm hooks commit. Claim still dispatches explicit compaction
requests.

Deps injected via `runtime.context: MeivaContext` (see agent/graph/_context.py).
agent_id read from RunnableConfig (LangGraph checkpointer standard).

State type hint (`state: _state.AgentState`): static only; see `agent/graph/exec/node.py` — the
graph build, not the annotation, decides which state class a node receives.

Refactored (2026-08): _Routing + resolve_routing, BatchState + per-kind
handlers, _Outcome + decide extracted; display logic moved to _present.py;
file split by axis under Task #1006. cc 70 → ~8-10 per module.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.db import ClaimedInbound, claim_inbound_batch
from agent.graph._attach_drain import build_attach_drain
from agent.graph.node_log import StallConfig, flush_node_exit_aggregate, node_lifecycle
from agent.impersonation import claim_gate
from agent.impersonation_handoff import resume_note_pending
from agent.messages import has_conversation
from agent.nodes import BEFORE_LLM, CLAIM, END
from agent.ownership.inbound import RuntimeOwnershipLostError
from base.agents.context import MeivaContext, agent_id_from_config
from base.agents.history.harness_messages import (
    aappend_harness_timeline_items,
    aload_harness_message_count,
)
from base.agents.history.timeline import build_timeline_items
from base.agents.messages.kwargs import read_meiva_kwargs
from base.api_contracts.agent_contract import Harness
from base.db.transaction import async_write_transaction
from base.events.live.projection import TimelineSnapshot

from ._decide import decide
from ._dispatch import BatchState, dispatch_batch, resolve_non_human_wake

# Names moved to co-located submodules during the Task #1006 split, re-exported
# here so existing `from agent.graph.claim.node import ...` call sites (tests)
# keep working unchanged. New code should import from the submodule that owns them.
from ._dispatch import by_who as by_who
from ._dispatch import handle_heartbeat as handle_heartbeat
from ._present import publish_end_timeline_snapshot, publish_inbound_committed
from ._routing import ClaimGoto, resolve_routing, route_target
from .harness_turn import render_messages, turn_key


def claim_will_idle(state: _state.AgentState, *, harness: Harness = Harness.MEIVA) -> bool:
    """The claim's single idle predicate — impl branch and wrapper snapshot share it.

    Idle when the turn already ended (`halted`), when the window carries no
    conversation yet AND no unprocessed end-of-session note is trailing (a
    resume note is the resumed input: `deliver_handoff` appends it and the
    claim must run its first turn even on a notes-only window), or when an
    open breaker parks the agent (`parks_idle`). The wrapper's turn-end
    full-window snapshot calls this same function, so the two decisions
    cannot drift.
    """
    if harness is not Harness.MEIVA:
        return not bool(state.harness_pending_input)
    return (
        state.halted
        or (not has_conversation(state.messages) and not resume_note_pending(state))
        or state.circuit.parks_idle
    )


async def claim_node_impl(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
) -> Command[ClaimGoto]:
    """Claim-node pipeline: container early-return → batch → routing → dispatch → decide.

    The original 590-line / cc=70 function is now a ~60-line / cc≈8 pipeline
    of extracted stages, each independently testable.
    """
    ctx = runtime.context

    # ── Container mode ──
    if ctx.ops_pool is None:
        if state.halted:
            return Command[ClaimGoto](goto=END)
        return Command[ClaimGoto](update={"halted": False}, goto=route_target(ctx, BEFORE_LLM))

    agent_id = agent_id_from_config(config)
    control = await claim_gate(state, agent_id, ctx)
    if control is not None:
        return control  # pyright: ignore[reportReturnType]

    # ── First SELECT: try uncontended claim before pub/sub wait ──
    try:
        batch = await claim_inbound_batch(
            ctx.ops_pool, agent_id, incarnation=ctx.original_incarnation
        )
    except RuntimeOwnershipLostError:
        return Command[ClaimGoto](update={"exit_requested": True}, goto=END)
    if not batch:
        # Every permanent rejection parks without fresh input. A healthy new
        # chat can try again after the cause is corrected.
        if claim_will_idle(state, harness=ctx.harness):
            drain = build_attach_drain(state, ctx)
            if drain is not None:
                return Command[ClaimGoto](update=drain, goto=CLAIM)
            if state.turn_active:
                # Turn boundary: one graph invocation = one turn. This
                # invocation already routed work (turn_active), and the next
                # thing to do is block for the next inbound — end the
                # invocation instead, so the runloop closes this turn's root
                # span and re-invokes on the same thread; the fresh
                # invocation's claim does the long wait. exit_requested stays
                # False: this END means "turn over", not "process exit".
                idle_reset = {} if state.interrupted_by_restart else {"non_human_wake": False}
                return Command[ClaimGoto](update={"turn_active": False, **idle_reset}, goto=END)
            # The host owns the idle agent and its subscription. End this
            # invocation; the dispatcher creates another task on the next wake.
            idle_reset = {} if state.interrupted_by_restart else {"non_human_wake": False}
            return Command[ClaimGoto](
                update={"turn_active": False, "turn_idle": True, **idle_reset}, goto=END
            )
        continuation_update: dict[str, object] = {"halted": False, "turn_active": True}
        if resume_note_pending(state):
            continuation_update["non_human_wake"] = False
        return Command[ClaimGoto](
            update=continuation_update,
            goto=route_target(ctx, BEFORE_LLM),
        )

    return await settle_claimed_batch(ctx, state, agent_id, batch)


async def _settle_external_harness_batch(
    ctx: MeivaContext,
    agent_id: int,
    st: BatchState,
    *,
    enters_harness_turn: bool,
    update: dict[str, object],
) -> None:
    update.pop("messages", None)
    if st.new_msgs and ctx.ops_pool is not None:
        items, _ = build_timeline_items(st.new_msgs, inputs=ctx.timestamp_policy())
        async with async_write_transaction(ctx.ops_pool) as pool_conn:
            persisted = await aappend_harness_timeline_items(
                pool_conn, agent_id, ctx.harness.value, items
            )
            total_count = await aload_harness_message_count(pool_conn, agent_id)
        if ctx.event_publisher is not None:
            snap = TimelineSnapshot(
                agent_id=agent_id,
                items=[it.model_dump() for it in persisted],
                msg_count=total_count,
            )
            ctx.event_publisher.emit(snap.model_dump_json())
    if enters_harness_turn:
        pending = [m for m in st.new_msgs if not isinstance(m, (AIMessage, ToolMessage))]
        text = render_messages(pending)
        key = turn_key(pending)
        inbound_ids = [
            id
            for m in st.new_msgs
            if isinstance(id := read_meiva_kwargs(m).get("meiva_inbound_id"), int)
        ]
        update["harness_pending_input"] = {
            "prompt": text,
            "key": key,
            "inbound_ids": inbound_ids,
        }


# meiva:note
# Everything the claim does once it holds a batch: resolve the batch's winner, dispatch every
# item through its handler, tell the frontend which chat inbounds were committed, decide the one
# Command, publish the end snapshot, and stamp the turn and exit channels. It takes the batch as a
# value, so the dispatch and veto rules are exercised with claimed rows of any kind without a claim
# in front of them.
async def settle_claimed_batch(
    ctx: MeivaContext,
    state: _state.AgentState,
    agent_id: int,
    batch: list[ClaimedInbound],
) -> Command[ClaimGoto]:
    routing = await resolve_routing(ctx, agent_id, batch)
    st = BatchState()
    await dispatch_batch(ctx, state, agent_id, batch, routing, st)
    await publish_inbound_committed(ctx, agent_id, st.committed_chat_ids)
    outcome = await decide(ctx, state, agent_id, st, routing)
    if outcome.publish_end_snapshot and ctx.harness is Harness.MEIVA:
        publish_end_timeline_snapshot(ctx, state, agent_id, st.new_msgs)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    # meiva:note
    # A dispatched batch means this invocation is mid-turn: stamp turn_active so a later claim
    # pass that finds nothing to do ends the invocation (the turn boundary) instead of blocking.
    # exit_requested carries the process-exit intent to the runloop and restart_requested the
    # hosted restart intent (drop the runtime, no exit-notify). Both key on the dispatch verdict
    # (st.next_goto == END: a terminate or restart won the batch), not on the command's own goto:
    # a compact co-batched with a terminate routes through INIT_CONTEXT first and only then
    # reaches END via reset.resume. The two channels are mutually exclusive by construction:
    # restart_requested is only ever set by the hosted restart branch, which never flips the row.
    update = dict(outcome.command.update or {})  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    update["turn_active"] = True
    update["non_human_wake"] = resolve_non_human_wake(state, batch, routing, st)
    update["exit_requested"] = (st.next_goto == END) and not st.restart_requested
    update["restart_requested"] = st.restart_requested
    if state.interrupted_by_restart:
        # meiva:note
        # The claim that takes the restart has read the mark; it describes one interrupt.
        update["interrupted_by_restart"] = False

    if ctx.harness is not Harness.MEIVA:
        await _settle_external_harness_batch(
            ctx,
            agent_id,
            st,
            enters_harness_turn=outcome.command.goto in (BEFORE_LLM, "harness_turn"),  # pyright: ignore[reportUnknownMemberType]
            update=update,
        )

    return Command[ClaimGoto](update=update, goto=route_target(ctx, outcome.command.goto))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType, reportArgumentType, reportCallIssue]


async def claim_node(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
) -> Command[ClaimGoto]:
    """Public entry point: wraps claim_node_impl with node-lifecycle logging.

    This is the node registered in the LangGraph state graph.  Its signature
    and return type are part of the public graph contract and MUST NOT change.
    """
    agent_id = agent_id_from_config(config)
    flush_node_exit_aggregate(agent_id)
    event_publisher = runtime.context.event_publisher
    assert event_publisher is not None, "claim_node requires ctx.event_publisher"  # noqa: S101
    # Turn-end fallback: when claim is about to block in _wait_for_batch
    # (idle), publish a FULL-WINDOW snapshot on enter — the only race-free
    # (in-memory, no checkpoint async-commit race) view of the finished turn.
    # This is what heals a frontend that missed events (SSE gap / dropped
    # deltas): the incremental snapshots in this design cover commits only, so
    # without it a reconnect GET could read a lagging checkpoint and the tail
    # of the turn would never appear. The shared `claim_will_idle` predicate is
    # the same call claim_node_impl's idle branch makes, so the two cannot drift.
    # If a batch wakes the
    # claim right after, the snapshot is still a legal view of the committed
    # state — the next node's incremental snapshot covers the new messages.
    will_idle = claim_will_idle(state, harness=runtime.context.harness)
    async with node_lifecycle(
        CLAIM,
        messages=state.messages,
        event_publisher=event_publisher,
        agent_id=agent_id,
        turn_progress=runtime.context.turn_progress,
        stall=StallConfig(runtime.context.require_agent().read("agent", "node_stall_dump_seconds")),
        full_window=will_idle,
        timeline_inputs=runtime.context.timestamp_policy(),
        timeline_limit=runtime.context.require_agent().read("display", "timeline_default_limit"),
        publish_timeline=runtime.context.harness is Harness.MEIVA,
    ):
        return await claim_node_impl(state, runtime, config)
