from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import suppress
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command
from pydantic import TypeAdapter

from agent import state as _state
from agent.db import ClaimedInbound
from agent.graph.claim._dispatch import is_human_wake_item, prepare_steered_inbound
from agent.graph.claim.harness_activity import HarnessTurnProjector
from agent.graph.claim.harness_foreground import Commit, Foreground, Handed, Requeue, Settlement
from agent.graph.claim.harness_inbox import (
    commit_handed,
    requeue_handed,
    reserve_head_batch,
    retire_unconfirmed,
)
from agent.graph.claim.harness_render import render_message, render_messages
from agent.graph.claim.harness_usage import log_turn_usage
from agent.graph.claim.lost_turn import report_lost_turn
from agent.graph.claim.timeline import append_turn_timeline
from agent.graph.claim.turn_stamp import persist_last_active
from agent.graph.hooks.user_delivery import capture_items, delivery_end_reminder
from agent.graph.interrupt import (
    ModelInterruptedError,
    interrupt_halt,
    interruptible_model,
    subscribe_interrupt,
)
from agent.graph.llm_errors import FatalLLMStreamError
from agent.graph.node_log import StallConfig, node_lifecycle
from agent.messages.reply_visibility import reply_visibility_note
from agent.nodes import CLAIM, HARNESS_TURN
from base.agents.context import MeivaContext, agent_id_from_config
from base.agents.history.harness_messages import (
    aget_next_harness_message_seq,
    aupsert_harness_timeline_item,
)
from base.agents.history.timeline import build_timeline_items
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.inbound import InterruptScope
from base.db.transaction import async_write_transaction
from base.events.live.projection import Cancelled, InboundCommitted, LLMDone, TimelineSnapshot
from base.events.live.publisher import AgentEventPublisher
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    EventSubscription,
    HarnessClosedError,
    HarnessInput,
    HarnessSetupError,
    HarnessTurnFaultedError,
    ReasoningDelta,
    ReasoningText,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnCompleted,
    completed_turn,
    fault_of_event,
)
from base.lm.harness.engine import TurnEngine
from base.log import logger

HarnessGoto = Literal["claim"]
_TIMELINE_ITEMS = TypeAdapter(list[TimelineItem])

# meiva:note
# This tick keeps host custody alive while a harness owns the invocation. Provider
# activity is recorded separately by the timeline projector.
# meiva:link agent/graph/docs/harness-turn.md
PROGRESS_TICK_SECONDS = 60.0

# meiva:note
# Delivery is woken by the host's inbound wake, so the queue is not polled while a turn runs. The
# fallback only bounds the cost of a lost wake. After a refusal (Antigravity refuses an input for a
# conversation that is just starting or just ending) the same input is not sent again before the
# backoff has passed: the next wake or fallback tick retries, never a loop.
# meiva:link agent/graph/docs/harness-turn.md
DELIVERY_FALLBACK_SECONDS = 15.0
REDELIVER_BACKOFF_SECONDS = 2.0


# meiva:note
# The graph invocation stays until nothing is left to settle: every input handed to the harness has
# entered its context (or gone back to the queue) and no turn is running. A turn ending settles
# nothing, and Claude Code reports an input that missed the last tool boundary about a second after
# the original turn, as a turn of its own. The invocation is also where that settlement is written, so it
# must still be here when the report arrives. Inputs are delivered one path: the head of the inbox,
# rendered, handed to `deliver()` after subscribing; idle or mid-turn is the harness's business.
# meiva:link agent/graph/docs/harness-turn.md
async def harness_turn_node(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
) -> Command[HarnessGoto]:
    ctx = runtime.context
    event_publisher = ctx.event_publisher
    assert event_publisher is not None, "harness_turn requires ctx.event_publisher"  # noqa: S101
    agent_id = agent_id_from_config(config)
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
        return await _run_harness_turn(state, ctx, agent_id, event_publisher)


def _apply_wake_polarity(
    state: _state.AgentState,
    projector: HarnessTurnProjector,
    updates: dict[str, Any],
) -> None:
    if state.non_human_wake and not projector.non_human_wake:
        updates["non_human_wake"] = False


_CLEARED: dict[str, Any] = {"harness_inputs": [], "harness_view": []}


async def _run_harness_turn(
    state: _state.AgentState,
    ctx: MeivaContext,
    agent_id: int,
    event_publisher: AgentEventPublisher,
) -> Command[HarnessGoto]:
    bridge = ctx.require_harness_bridge()
    ctx.turn_progress.mark(agent_id)
    extra_updates: dict[str, Any] = {}

    base_idx = 0
    if ctx.ops_pool is not None:
        async with ctx.ops_pool.connection() as pool_conn:
            base_idx = await aget_next_harness_message_seq(pool_conn, agent_id)

    async def checkpoint(item: TimelineItem, msg_count: int) -> None:
        await _checkpoint_activity(ctx, agent_id, event_publisher, item, msg_count)
        await capture_items(ctx, agent_id, [item], source_key="harness")

    projector = HarnessTurnProjector(
        checkpoint=checkpoint,
        base_idx=base_idx,
        clock=ctx.require_clock(),
        non_human_wake=state.non_human_wake,
        explicit_visibility=ctx.require_agent().prompt.user_delivery_mode == "prefix",
        delivery_mode=ctx.require_agent().prompt.user_delivery_mode,
    )
    run = ForegroundRun(ctx, agent_id, bridge, projector, state, event_publisher)

    try:
        await _await_foreground(ctx, agent_id, bridge, projector, run)
    except ModelInterruptedError as exc:
        await run.retire_unconfirmed()
        await projector.finish(interrupted=True)
        _apply_wake_polarity(state, projector, extra_updates)
        return Command[HarnessGoto](
            update={**extra_updates, **interrupt_halt(exc.reason), **_CLEARED}, goto=CLAIM
        )
    except (HarnessTurnFaultedError, HarnessClosedError, HarnessSetupError) as exc:
        if isinstance(exc, HarnessTurnFaultedError) and exc.outcome is not None:
            log_turn_usage(ctx, agent_id, bridge.model, exc.outcome)
        if not isinstance(exc, HarnessTurnFaultedError) or exc.outcome is None:
            await bridge.retire(reason="session unavailable")
        inbound_ids = run.unsettled_inbound_ids()
        await run.retire_unconfirmed()
        if projector.has_activity:
            await projector.finish(error=str(exc))
        else:
            await report_lost_turn(
                ctx, agent_id, inbound_ids, run.last_key, exc, append=append_turn_timeline
            )
        raise FatalLLMStreamError(f"{ctx.harness.value} harness turn aborted: {exc}") from exc

    await projector.finish(interrupted=run.interrupted)
    _apply_wake_polarity(state, projector, extra_updates)
    if run.interrupted:
        _announce_cancel(ctx, agent_id)
    return Command[HarnessGoto](update={**extra_updates, "halted": True, **_CLEARED}, goto=CLAIM)


# meiva:note
# One invocation's foreground: deliver the inbox, settle what the harness reports.
#
# Two tasks share it, both on one event loop and both yielding only at awaits: the event reader folds
# each event into the pure `Foreground` and writes its settlements, and the delivery loop hands the head
# of the inbox over when it can and decides when the invocation may end.
class ForegroundRun:
    def __init__(
        self,
        ctx: MeivaContext,
        agent_id: int,
        bridge: TurnEngine,
        projector: HarnessTurnProjector,
        state: _state.AgentState,
        publisher: AgentEventPublisher,
    ) -> None:
        self._ctx = ctx
        self._agent_id = agent_id
        self._bridge = bridge
        self._projector = projector
        self._publisher = publisher
        self._baseline = state.user_delivery_baseline
        self.fg = Foreground()
        self._synthetic: list[tuple[Handed, str]] = []
        self._prompts: dict[str, str] = {}
        for raw in state.harness_inputs:
            key, prompt = str(raw["key"]), str(raw["prompt"])
            items = _TIMELINE_ITEMS.validate_python(raw.get("timeline_items", []))
            self._synthetic.append((Handed(key, None, tuple(items)), prompt))
        self.last_key = f"inbound-turn-{agent_id}"
        self.interrupted = False
        self._correcting = False
        self._turn_ended = False
        self._streamed: dict[str, list[str]] = {}
        self._changed = asyncio.Event()
        self._backoff_until = 0.0
        self._failure: Exception | None = None

    def unsettled_inbound_ids(self) -> list[int]:
        return [h.inbound_id for h in self.fg.handed if h.inbound_id is not None]

    async def retire_unconfirmed(self) -> None:
        items = await self._projector.append_inbound(
            lambda: retire_unconfirmed(self._ctx, self._agent_id, self.fg.handed)
        )
        _publish_inbound(self._ctx, self._agent_id, items)

    async def execute(self) -> None:
        subscription = await self._bridge.subscribe()
        outcome: BaseException | None = None
        try:
            async with asyncio.TaskGroup() as tasks:
                reader = tasks.create_task(
                    self._read(subscription), name=f"harness-read-{self._agent_id}"
                )
                waker = tasks.create_task(self._wake(), name=f"harness-wake-{self._agent_id}")
                try:
                    await self._deliver_until_quiet()
                except BaseException as error:
                    outcome = error
                finally:
                    _ = reader.cancel()
                    _ = waker.cancel()
        finally:
            subscription.close()
        if outcome is not None:
            raise outcome
        if self._failure is not None:
            raise self._failure

    async def _read(self, subscription: EventSubscription) -> None:
        try:
            while (event := await subscription.get()) is not None:
                await self._on_event(event)
            self._failure = HarnessClosedError(
                "harness event stream ended while inputs were in flight"
            )
        except Exception as exc:
            self._failure = exc
        self._changed.set()

    async def _wake(self) -> None:
        while True:
            await self._bridge.wait_inbound(DELIVERY_FALLBACK_SECONDS)
            self._changed.set()

    async def _on_event(self, event: BridgeEvent) -> None:
        if (fault := fault_of_event(self._ctx.harness, event)) is not None:
            raise fault
        match event:
            case AssistantText(turn_id=turn_id, text=text):
                self._streamed.setdefault(turn_id, []).append(text)
                await self._projector.on_event(event)
            case ReasoningDelta() | ReasoningText() | TextDelta() | ToolUse() | ToolResult():
                await self._projector.on_event(event)
            case TurnCompleted():
                await self._on_turn_completed(event)
            case _:
                pass
        self.fg, settlements = self.fg.on_event(event)
        for settlement in settlements:
            await self._settle(settlement)
        self._changed.set()

    async def _on_turn_completed(self, event: TurnCompleted) -> None:
        turn = completed_turn(event, "".join(self._streamed.pop(event.turn_id, [])))
        log_turn_usage(self._ctx, self._agent_id, self._bridge.model, turn)
        await self._projector.finish((turn,), interrupted=turn.interrupted)
        self._turn_ended = True
        if turn.interrupted:
            self.interrupted = True
            return
        self._publisher.emit(LLMDone(agent_id=self._agent_id).model_dump_json())
        await persist_last_active(self._ctx, self._agent_id, turn.text)

    async def _settle(self, settlement: Settlement) -> None:
        match settlement:
            case Commit(handed=handed):
                items = await self._projector.append_inbound(
                    lambda: commit_handed(self._ctx, self._agent_id, [handed]),
                    human_input=handed.human,
                )
                _publish_inbound(self._ctx, self._agent_id, items)
            case Requeue(handed=handed, reason=reason):
                logger.info(
                    "harness input returned to the queue",
                    event="harness_input_requeued",
                    agent_id=self._agent_id,
                    key=handed.key,
                    reason=reason,
                )
                self._backoff_until = asyncio.get_running_loop().time() + REDELIVER_BACKOFF_SECONDS
                if handed.inbound_id is None:
                    self._synthetic.insert(0, (handed, self._prompts[handed.key]))
                else:
                    await requeue_handed(self._ctx, self._agent_id, [handed])

    async def _deliver_until_quiet(self) -> None:
        while self._failure is None:
            self._changed.clear()
            if await self._deliver_ready():
                continue
            backoff = self._backoff_until - asyncio.get_running_loop().time()
            if self.fg.quiet and not self._synthetic and backoff <= 0:
                if not await self._queue_correction():
                    return
                continue
            timeout = backoff if backoff > 0 else DELIVERY_FALLBACK_SECONDS
            with suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout)

    # meiva:note
    # Reserve the head of the inbox, unless a refusal is still backing off. A refusal can be folded in
    # while the reservation runs; what was just reserved is then handed back unsent, so the refused
    # inputs keep their place ahead of anything newer.
    # meiva:link agent/graph/docs/harness-turn.md
    async def _reserve(self) -> list[ClaimedInbound]:
        rows = await reserve_head_batch(self._ctx, self._agent_id)
        if rows and asyncio.get_running_loop().time() < self._backoff_until:
            await self._give_back(rows)
            return []
        return rows

    async def _give_back(self, rows: Sequence[ClaimedInbound]) -> None:
        await requeue_handed(
            self._ctx, self._agent_id, [Handed(f"inbound-{r.id}", r.id, ()) for r in rows]
        )

    async def _render(self, rows: Sequence[ClaimedInbound]) -> list[tuple[Handed, str]]:
        rendered: list[tuple[Handed, str]] = []
        for item in rows:
            try:
                messages = prepare_steered_inbound(self._ctx, item)
                text = render_messages(messages)
                items, _ = build_timeline_items(messages, inputs=self._ctx.timestamp_policy())
            except Exception:
                await self._give_back(rows)
                raise
            entry = Handed(
                f"inbound-{item.id}", item.id, tuple(items), human=is_human_wake_item(item)
            )
            rendered.append((entry, text))
        return rendered

    async def _deliver_ready(self) -> bool:
        if asyncio.get_running_loop().time() < self._backoff_until:
            return False
        rows = await self._reserve()
        synthetic, self._synthetic = self._synthetic, []
        handed: list[Handed] = []
        inputs: list[HarnessInput] = []
        for entry, prompt in synthetic:
            self._prompts[entry.key] = prompt
            handed.append(entry)
            inputs.append(HarnessInput(key=entry.key, text=prompt))
        settled_now: list[Handed] = []
        for entry, text in await self._render(rows):
            if text.strip():
                handed.append(entry)
                inputs.append(HarnessInput(key=entry.key, text=text))
            else:
                settled_now.append(entry)
        if not handed and not settled_now:
            return False
        if settled_now:
            items = await self._projector.append_inbound(
                lambda: commit_handed(self._ctx, self._agent_id, settled_now)
            )
            _publish_inbound(self._ctx, self._agent_id, items)
        if not handed:
            return True
        self.last_key = handed[-1].key
        self.fg = self.fg.hand(tuple(handed))
        try:
            await self._bridge.deliver(inputs)
        except (HarnessClosedError, HarnessSetupError, ModelInterruptedError):
            raise
        except Exception as exc:
            raise HarnessSetupError(
                f"{self._ctx.harness.value} could not take the input: {exc}"
            ) from exc
        return True

    async def _queue_correction(self) -> bool:
        if not self._turn_ended or self.interrupted:
            return False
        self._turn_ended = False
        invalid, self._projector.invalid_reply_visibility = (
            self._projector.invalid_reply_visibility,
            False,
        )
        correction = await harness_reply_correction(
            self._ctx,
            self._agent_id,
            self._baseline,
            invalid=invalid,
            key=self.last_key,
            correcting=self._correcting,
        )
        if correction is None:
            return False
        self._correcting = True
        self._synthetic.append(correction)
        return True


# meiva:note
# The correction input for a reply that broke the delivery contract, or None.
#
# The correction is a synthetic input like any other: its note reaches the timeline when the harness
# reports it in context. Asking twice would loop, so a second violation is only recorded.
async def harness_reply_correction(
    ctx: MeivaContext,
    agent_id: int,
    baseline: int | None,
    *,
    invalid: bool,
    key: str,
    correcting: bool,
) -> tuple[Handed, str] | None:
    note = await delivery_end_reminder(ctx, agent_id, baseline, correcting=correcting)
    if not invalid and note is None:
        return None
    note = note or reply_visibility_note(ctx.require_clock().now())
    if correcting:
        await append_turn_timeline(ctx, agent_id, [note])
        return None
    items, _ = build_timeline_items([note], inputs=ctx.timestamp_policy())
    entry = Handed(f"{key}:visibility-correction", None, tuple(items))
    return entry, render_message(note)


async def _await_foreground(
    ctx: MeivaContext,
    agent_id: int,
    bridge: TurnEngine,
    projector: HarnessTurnProjector,
    run: ForegroundRun,
) -> None:
    resources = ctx.hosted_resources
    async with (
        subscribe_interrupt(
            ctx.ops_pool, agent_id, resources=resources, scope=InterruptScope.LIFECYCLE
        ) as lifecycle,
        subscribe_interrupt(
            ctx.ops_pool, agent_id, resources=resources, scope=InterruptScope.FOREGROUND
        ) as cancel,
    ):

        async def request_cancel() -> None:
            await cancel.wait()
            if error := await interrupt_engine(agent_id, bridge):
                await projector.record_control(f"Cancellation unconfirmed: {error}", error=True)

        async def go() -> None:
            await _with_cancel(run.execute, request_cancel, agent_id)

        await interruptible_model(
            _ticking(ctx.turn_progress.mark, agent_id, go(), projector), lifecycle
        )


# meiva:note
# A foreground cancel sends a control request while the event reader remains alive. Only the harness's
# own terminal event ends a turn, so a cancel changes nothing about when the invocation ends: it ends
# when everything handed over is settled and no turn runs. Inputs not yet in the context at that moment
# are still delivered and settled; they are delivered as a new turn, as in the built-in runtime.
# meiva:link agent/graph/docs/harness-turn.md
async def _with_cancel(
    execute: Callable[[], Awaitable[None]],
    request_cancel: Callable[[], Coroutine[Any, Any, None]],
    agent_id: int,
) -> None:
    outcome: BaseException | None = None
    async with asyncio.TaskGroup() as tasks:
        cancel_task = tasks.create_task(request_cancel(), name=f"harness-cancel-{agent_id}")
        try:
            await execute()
        except BaseException as error:
            outcome = error
        finally:
            cancel_task.cancel()
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
    if outcome is not None:
        raise outcome


def _publish_inbound(ctx: MeivaContext, agent_id: int, items: list[TimelineItem]) -> None:
    publisher = ctx.event_publisher
    if publisher is None or not items:
        return
    count = int(items[-1].item_id.split(".")[0].removeprefix("h")) + 1
    publisher.emit(
        TimelineSnapshot(
            agent_id=agent_id, items=[item.model_dump() for item in items], msg_count=count
        ).model_dump_json()
    )
    for item in items:
        if item.kind == "inbound_chat" and item.inbound_id is not None:
            publisher.emit(
                InboundCommitted(agent_id=agent_id, inbound_id=item.inbound_id).model_dump_json()
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
# The waited work is captured and raised after the group closes, the way `interruptible_model` does
# it: a TaskGroup wraps a lone body error in an ExceptionGroup, and the node matches a faulted session on
# the error's own type, so the identity has to survive this scope. A cancelled wait keeps its
# CancelledError for the same reason.
# meiva:link agent/graph/docs/harness-turn.md
async def _ticking(
    mark: Callable[[int], None],
    agent_id: int,
    work: Awaitable[None],
    projector: HarnessTurnProjector,
) -> None:
    async def settle() -> BaseException | None:
        try:
            await work
        except BaseException as error:
            return error
        return None

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
    if outcome is not None:
        raise outcome


# meiva:note
# Tell the live view the turn ended. Its committed activities remain visible and
# the durable cancel stays pending for the next claim pass.
# meiva:link agent/graph/docs/harness-turn.md
def _announce_cancel(ctx: MeivaContext, agent_id: int) -> None:
    assert ctx.event_publisher is not None, "a cancelled harness turn requires ctx.event_publisher"  # noqa: S101
    ctx.event_publisher.emit(Cancelled(agent_id=agent_id).model_dump_json())


# meiva:note
# An interrupt that cannot be delivered is reported, never raised: the durable cancel row is still
# pending and claim dispatches it on the next pass, while a raise here would turn the user's stop into
# a failed turn. Process observations are diagnostics. The foreground ends only when the executor
# reports a terminal outcome.
# meiva:link agent/graph/docs/harness-turn.md
async def interrupt_engine(agent_id: int, bridge: TurnEngine) -> str | None:
    try:
        report = await bridge.interrupt()
    except Exception as exc:
        logger.warning(
            "[{label}] {body}",
            label="harness",
            event="harness_interrupt_failed",
            body=f"interrupting agent {agent_id}'s harness turn raised: {exc!r}",
        )
        return str(exc) or type(exc).__name__
    logger.info(
        "[{label}] {body}",
        label="harness",
        event="harness_interrupt",
        body=f"requested interruption for agent {agent_id}'s harness turn",
        agent_id=agent_id,
        settled=getattr(report, "settled", None),
        turn_id=getattr(report, "turn_id", None),
    )
    return None
