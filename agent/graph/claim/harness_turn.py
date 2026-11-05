from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.graph.claim.harness_activity import HarnessTurnProjector
from agent.graph.claim.harness_tool_format import is_object_sequence
from agent.graph.claim.lost_turn import report_lost_turn
from agent.graph.claim.timeline import append_turn_timeline
from agent.graph.claim.turn_stamp import persist_last_active
from agent.graph.hooks.framework import harness_pre_turn_hooks
from agent.graph.interrupt import (
    ModelInterruptedError,
    interruptible_model,
    subscribe_interrupt,
)
from agent.graph.llm_errors import FatalLLMStreamError
from agent.graph.node_log import StallConfig, node_lifecycle
from agent.hooks import call_graph_hook
from agent.nodes import CLAIM, HARNESS_TURN
from base.agents.context import MeivaContext, agent_id_from_config
from base.agents.history.harness_messages import (
    aget_next_harness_message_seq,
    aupsert_harness_timeline_item,
)
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.kwargs import read_meiva_kwargs
from base.db.transaction import async_write_transaction
from base.events.live.projection import Cancelled, LLMDone, TimelineSnapshot
from base.events.live.publisher import AgentEventPublisher
from base.host.boundary.json_values import is_object_dict
from base.lm.catalog import ModelCatalog
from base.lm.content import message_content
from base.lm.harness import HarnessClosedError, HarnessSetupError, HarnessTurnFaultedError
from base.lm.harness.engine import HarnessTurn, TurnEngine
from base.lm.usage import UsageFields, log_usage_fields
from base.log import logger

HarnessGoto = Literal["claim"]

# meiva:note
# This tick keeps host custody alive while a harness owns the invocation. Provider
# activity is recorded separately by the timeline projector.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
PROGRESS_TICK_SECONDS = 60.0


# meiva:note
# Filter out agent replies from a sequence of messages.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
def pending_messages(
    messages_or_state: _state.AgentState | Sequence[BaseMessage],
) -> list[BaseMessage]:
    messages = (
        messages_or_state.messages
        if isinstance(messages_or_state, _state.AgentState)
        else messages_or_state
    )
    return [message for message in messages if not isinstance(message, (AIMessage, ToolMessage))]


def render_messages(messages: Sequence[BaseMessage]) -> str:
    return "\n\n".join(render_message(message) for message in messages).strip()


def render_message(message: BaseMessage) -> str:
    content = message_content(message)
    if isinstance(content, str):
        return content
    return "\n".join(
        block
        if isinstance(block, str)
        else (
            str(block["text"])
            if block.get("type") == "text" and isinstance(block.get("text"), str)
            else f"[{block.get('type', 'attachment')}]"
        )
        for block in content
    )


# meiva:note
# The adapter's idempotency key: the newest inbound row in the batch, which is unique
# per claim and stable when the same window is submitted twice (a crashed turn, a
# retry within the session). A window that carries only notes has no row to name, so
# the cursor plus its length stands in; retrying the same window at the same cursor
# reuses the key, while a later note-only turn (such as a second heartbeat) gets a
# distinct key instead of replaying the first.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
def turn_key(messages: Sequence[BaseMessage], *, cursor: int = 0) -> str:
    for message in reversed(messages):
        inbound_id = read_meiva_kwargs(message).get("meiva_inbound_id")
        if isinstance(inbound_id, int):
            return f"inbound-{inbound_id}"
    return f"window-{cursor + len(messages)}"


async def _run_pre_turn_hooks(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
) -> tuple[list[BaseMessage], dict[str, Any]]:
    ctx = runtime.context
    if ctx.ops_pool is None:
        return [], {}
    pre_messages: list[BaseMessage] = []
    hook_updates: dict[str, Any] = {}
    for _plugin, hook in harness_pre_turn_hooks(ctx.plugin_registry()):
        result = await call_graph_hook(hook, state, runtime, config)
        if not is_object_dict(result) or not result:
            continue
        for key, value in result.items():
            if key == "messages" and is_object_sequence(value):
                pre_messages.extend(m for m in value if isinstance(m, BaseMessage))
            elif key != "goto":
                hook_updates[key] = value
    return pre_messages, hook_updates


# meiva:note
# The graph waits for one big turn. Observable activities are committed by the
# projector while the harness works, independently of the terminal outcome.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def harness_turn_node(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
) -> Command[HarnessGoto]:
    ctx = runtime.context
    event_publisher = ctx.event_publisher
    assert event_publisher is not None, "harness_turn requires ctx.event_publisher"  # noqa: S101
    agent_id = agent_id_from_config(config)
    pre_messages, hook_updates = await _run_pre_turn_hooks(state, runtime, config)
    async with node_lifecycle(
        HARNESS_TURN,
        messages=state.messages,
        event_publisher=event_publisher,
        agent_id=agent_id,
        turn_progress=ctx.turn_progress,
        stall=StallConfig(ctx.require_agent().read("agent", "node_stall_dump_seconds")),
        timeline_inputs=ctx.timestamp_policy(),
        timeline_limit=ctx.require_agent().read("display", "timeline_default_limit"),
        publish_timeline=False,
    ):
        return await _run_harness_turn(
            state,
            ctx,
            agent_id,
            event_publisher,
            pre_messages=pre_messages,
            hook_updates=hook_updates,
        )


def _resolve_turn_prompt_and_key(
    state: _state.AgentState,
    pre_messages: Sequence[BaseMessage],
    agent_id: int,
) -> tuple[str, str]:
    pending_input = state.harness_pending_input or {}
    text = str(pending_input.get("prompt") or "")
    if pre_messages:
        pre_text = render_messages(pre_messages)
        text = f"{pre_text}\n\n{text}".strip() if text else pre_text
    key = str(pending_input.get("key") or "") or f"inbound-turn-{agent_id}"
    return text, key


async def _handle_turn_interrupted(
    ctx: MeivaContext,
    agent_id: int,
    bridge: TurnEngine,
    projector: HarnessTurnProjector,
) -> None:
    await projector.finish(interrupted=True)
    _announce_cancel(ctx, agent_id)
    await interrupt_engine(agent_id, bridge)


async def _run_harness_turn(
    state: _state.AgentState,
    ctx: MeivaContext,
    agent_id: int,
    event_publisher: AgentEventPublisher,
    *,
    pre_messages: Sequence[BaseMessage] = (),
    hook_updates: Mapping[str, Any] | None = None,
) -> Command[HarnessGoto]:
    bridge = ctx.require_harness_bridge()
    ctx.turn_progress.mark(agent_id)
    extra_updates = dict(hook_updates) if hook_updates else {}
    text, key = _resolve_turn_prompt_and_key(state, pre_messages, agent_id)
    if not text:
        return Command[HarnessGoto](
            update={**extra_updates, "halted": True, "harness_pending_input": None}, goto=CLAIM
        )

    base_idx = 0
    if ctx.ops_pool is not None:
        async with ctx.ops_pool.connection() as pool_conn:
            base_idx = await aget_next_harness_message_seq(pool_conn, agent_id)

    async def checkpoint(item: TimelineItem, msg_count: int) -> None:
        await _checkpoint_activity(ctx, agent_id, event_publisher, item, msg_count)

    projector = HarnessTurnProjector(
        checkpoint=checkpoint,
        base_idx=base_idx,
        clock=ctx.require_clock(),
        non_human_wake=state.non_human_wake,
    )
    try:
        async with subscribe_interrupt(
            ctx.ops_pool, agent_id, resources=ctx.hosted_resources
        ) as cancel_event:
            action = bridge.run_turn(text, key=key, on_event=projector.on_event)
            turn = await interruptible_model(
                _ticking(ctx.turn_progress.mark, agent_id, action, projector), cancel_event
            )
    except ModelInterruptedError:
        await _handle_turn_interrupted(ctx, agent_id, bridge, projector)
        return Command[HarnessGoto](
            update={**extra_updates, "halted": True, "harness_pending_input": None}, goto=CLAIM
        )
    except (HarnessTurnFaultedError, HarnessClosedError, HarnessSetupError) as exc:
        outcome = exc.outcome if isinstance(exc, HarnessTurnFaultedError) else None
        if outcome is not None:
            log_turn_usage(ctx, agent_id, bridge.model, outcome)
        if projector.has_activity:
            await projector.finish(error=str(exc))
        else:
            await report_lost_turn(
                ctx, agent_id, state.harness_pending_input, key, exc, append=append_turn_timeline
            )
        raise FatalLLMStreamError(f"{ctx.harness.value} harness turn aborted: {exc}") from exc

    turns = (*turn.prelude, turn)
    for completed in turns:
        log_turn_usage(ctx, agent_id, bridge.model, completed)
    await projector.finish(turns, interrupted=turn.interrupted)
    if turn.interrupted:
        _announce_cancel(ctx, agent_id)
    else:
        event_publisher.emit(LLMDone(agent_id=agent_id).model_dump_json())
        await persist_last_active(ctx, agent_id, turn.text)
    return Command[HarnessGoto](
        update={**extra_updates, "halted": True, "harness_pending_input": None}, goto=CLAIM
    )


async def _checkpoint_activity(
    ctx: MeivaContext,
    agent_id: int,
    publisher: AgentEventPublisher,
    item: TimelineItem,
    msg_count: int,
) -> None:
    if ctx.ops_pool is not None:
        async with async_write_transaction(ctx.ops_pool) as conn:
            saved = await aupsert_harness_timeline_item(
                conn, agent_id, ctx.harness, item, incarnation=ctx.original_incarnation
            )
            if saved is None:
                raise RuntimeError(
                    f"Harness activity checkpoint rejected for agent {agent_id}: {item.item_id}"
                )
    publisher.emit(
        TimelineSnapshot(
            agent_id=agent_id, items=[item.model_dump()], msg_count=msg_count
        ).model_dump_json()
    )


# meiva:note
# The waited turn is captured and raised after the group closes, the way
# `interruptible_model` does it: a TaskGroup wraps a lone body error in an
# ExceptionGroup, and the node matches a faulted session on the error's own type, so
# the identity has to survive this scope. A cancelled turn keeps its CancelledError
# for the same reason.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def _ticking(
    mark: Callable[[int], None],
    agent_id: int,
    turn: Awaitable[HarnessTurn],
    projector: HarnessTurnProjector,
) -> HarnessTurn:
    async def settle() -> HarnessTurn | BaseException:
        try:
            return await turn
        except BaseException as error:
            return error

    async def tick() -> None:
        while True:
            await asyncio.sleep(PROGRESS_TICK_SECONDS)
            mark(agent_id)

    async with asyncio.TaskGroup() as tasks:
        ticker = tasks.create_task(tick(), name=f"harness-progress-{agent_id}")
        checkpoints = tasks.create_task(projector.flush_text(), name=f"harness-timeline-{agent_id}")
        outcome = await settle()
        ticker.cancel()
        checkpoints.cancel()
        with suppress(asyncio.CancelledError):
            await ticker
        with suppress(asyncio.CancelledError):
            await checkpoints
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


# meiva:note
# Tell the live view the turn ended. Its committed activities remain visible and
# the durable cancel stays pending for the next claim pass.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
def _announce_cancel(ctx: MeivaContext, agent_id: int) -> None:
    assert ctx.event_publisher is not None, "a cancelled harness turn requires ctx.event_publisher"  # noqa: S101
    ctx.event_publisher.emit(Cancelled(agent_id=agent_id).model_dump_json())


# meiva:note
# An interrupt that cannot be delivered is reported, never raised: the durable cancel
# row is still pending and claim dispatches it on the next pass, while a raise here
# would turn the user's stop into a failed turn. The adapter's `settled` is the one
# honest answer to "did the child processes actually stop", so it is logged as it came
# back — `None` included, which means the CLI tree was not observable rather than reaped.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def interrupt_engine(agent_id: int, bridge: TurnEngine) -> None:
    try:
        report = await bridge.interrupt()
    except Exception as exc:
        logger.warning(
            "[{label}] {body}",
            label="harness",
            event="harness_interrupt_failed",
            body=f"interrupting agent {agent_id}'s harness turn raised: {exc!r}",
        )
        return
    logger.info(
        "[{label}] {body}",
        label="harness",
        event="harness_interrupt",
        body=f"interrupted agent {agent_id}'s harness turn",
        agent_id=agent_id,
        settled=getattr(report, "settled", None),
        turn_id=getattr(report, "turn_id", None),
    )


# meiva:note
# Usage is logged where every other agent turn logs it, with the harness name as the
# `source` so the two runtimes stay comparable. A turn whose provider reported no
# tokens is not logged at all: an all-zero row would read as a free call rather than
# as a provider that said nothing.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
def _resolve_pricing_model(raw_model: str, catalog: ModelCatalog) -> str:
    candidate = raw_model.removesuffix("[1m]")
    for suffix in ("", "-high", "-medium", "-low", "-minimal"):
        if candidate.endswith(suffix) and (s := candidate.removesuffix(suffix)) in catalog.prices:
            return raw_model if not suffix and raw_model in catalog.prices else s
    return raw_model


def log_turn_usage(ctx: MeivaContext, agent_id: int, model: str | None, turn: HarnessTurn) -> None:
    usage = turn.usage
    if turn.replayed or (usage.input_tokens == 0 and usage.output_tokens == 0):
        return
    catalog = ctx.require_catalog()
    pricing_model = _resolve_pricing_model(model or ctx.require_agent().brain.llm_model, catalog)
    entries = (
        [(step, 1) for step in turn.steps if step.input_tokens > 0 or step.output_tokens > 0]
        if turn.steps
        else [(usage, max(turn.num_turns, 1))]
    )
    try:
        for step_usage, calls in entries:
            fields = UsageFields(
                tok_in=step_usage.input_tokens,
                tok_out=step_usage.output_tokens,
                tok_cached=step_usage.cache_read_input_tokens or None,
                cache_write_5m=step_usage.cache_creation_input_tokens,
                source=ctx.harness.value,
                for_agent_id=agent_id,
                calls=calls,
            )
            log_usage_fields(pricing_model, fields, usage_kind="agent", catalog=catalog)
    except Exception as error:
        # meiva:note
        # A bill that cannot be priced is a hole in an accounting row; the reply it
        # describes is real either way. The turn has already finished by the time it
        # is logged, so a raise here deletes a reply the user paid for — a provider
        # that reports token counts in a shape the price book rejects must not cost
        # the answer, and the recorded warning keeps the gap visible.
        # meiva:link agent/graph/docs/harness-turn.meiva.okf.md
        logger.warning(
            "[{label}] {body}",
            label="harness",
            event="harness_usage_log_failed",
            body=(
                f"agent {agent_id}'s {ctx.harness.value} turn finished but its usage could not be "
                f"priced: {error!r}"
            ),
            agent_id=agent_id,
            tok_in=usage.input_tokens,
            tok_out=usage.output_tokens,
            tok_cached=usage.cache_read_input_tokens,
            cache_write=usage.cache_creation_input_tokens,
        )
