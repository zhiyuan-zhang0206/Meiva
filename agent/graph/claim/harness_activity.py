from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from agent.graph.claim.harness_tool_format import format_tool_use
from agent.graph.hooks.user_delivery import capture_items
from base.agents.context import MeivaContext
from base.agents.history.harness_messages import (
    aget_harness_call_item_id,
    aget_next_harness_message_seq,
    aupsert_harness_timeline_item,
)
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.reply_intent import parse_reply_visibility, resolve_agent_reply
from base.agents.sdk.telemetry import SdkCall
from base.clock import Clock
from base.db.transaction import async_write_transaction
from base.events.live.projection import TimelineSnapshot
from base.events.live.publisher import AgentEventPublisher
from base.lm.catalog import ModelCatalog
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    ReasoningDelta,
    ReasoningText,
    SubagentText,
    TextDelta,
    ToolResult,
    ToolUse,
)
from base.lm.harness.engine import HarnessTurn
from base.lm.usage import UsageFields, log_usage_fields
from base.log import logger

TEXT_CHECKPOINT_SECONDS = 0.2

# meiva:note
# Sub-agent text is work detail a CLI can produce without bound, so one message, tool call input or
# tool output is cut at `SUBAGENT_ITEM_CHARS`. Every item is still written; there is no per-run total.
# The main agent's own text and tool output are not cut here; the cut is only for the part a person
# never asked the agent to produce.
# meiva:link base/lm/harness/docs/subagent-text.md
SUBAGENT_ITEM_CHARS = 8_000
SUBAGENT_CUT = "\n[... sub-agent text cut]"


def _elapsed(start: datetime, end: datetime) -> int:
    return max(int((end - start).total_seconds() * 1000), 0)


# meiva:note
# Checkpoint each observed activity under one stable timeline position.
# meiva:link agent/graph/docs/harness-turn.md
class HarnessTurnProjector:
    def __init__(
        self,
        *,
        checkpoint: Callable[[TimelineItem, int], Awaitable[None]],
        base_idx: int,
        clock: Clock,
        non_human_wake: bool = False,
        explicit_visibility: bool = False,
        delivery_mode: str | None = None,
        find_call: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self._checkpoint = checkpoint
        self._next_idx = self._base_idx = self._committed_count = base_idx
        self._clock = clock
        self._non_human_wake = non_human_wake
        self._explicit_visibility = explicit_visibility
        self._delivery_mode = delivery_mode
        self.invalid_reply_visibility = False
        self._activity_started = clock.now()
        self._text_started = self._activity_started
        self._reasoning_ended: datetime | None = None
        self._text_item: TimelineItem | None = None
        self._text = ""
        self._has_assistant_text = False
        self._last_chat_raw = ""
        self._tools: dict[str, tuple[TimelineItem, TimelineItem, int, datetime]] = {}
        self._seen_tools: set[str] = set()
        self._tool_items: dict[str, str] = {}
        self._find_call = find_call
        self._unfound_calls: set[str] = set()
        self._pending: dict[str, TimelineItem] = {}
        self._dirty = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def has_activity(self) -> bool:
        return self._next_idx > self._base_idx

    @property
    def non_human_wake(self) -> bool:
        return self._non_human_wake

    async def on_event(self, event: BridgeEvent) -> None:
        async with self._lock:
            now = self._clock.now()
            match event:
                case ReasoningDelta() | ReasoningText() | TextDelta() | AssistantText():
                    await self._on_text(event, now)
                case SubagentText():
                    await self._on_subagent_text(event, now)
                    await self._flush()
                case ToolUse():
                    await self._on_tool_use(event, now)
                    await self._flush()
                case ToolResult():
                    self._on_tool_result(event, now)
                    await self._flush()
                case _:
                    raise TypeError(f"unexpected harness activity: {type(event).__name__}")

    async def record_control(self, text: str, *, error: bool = False) -> None:
        async with self._lock:
            self._last_chat_raw = ""
            prefix = "error:" if error else "status:"
            self._new_item("system_marker", f"{prefix}{text}", self._clock.now())
            await self._flush()

    async def append_inbound(
        self,
        commit: Callable[[], Awaitable[list[TimelineItem]]],
        *,
        human_input: bool = False,
    ) -> list[TimelineItem]:
        async with self._lock:
            self._last_chat_raw = ""
            await self._flush()
            items = await commit()
            for item in items:
                if item.item_id not in (f"h{self._next_idx}.0", f"{self._next_idx}.0"):
                    raise RuntimeError(f"unexpected harness inbound position {item.item_id}")
                self._next_idx += 1
            self._committed_count = self._next_idx
            if human_input:
                self._non_human_wake = False
            return items

    async def flush_text(self) -> None:
        while True:
            await self._dirty.wait()
            await asyncio.sleep(TEXT_CHECKPOINT_SECONDS)
            async with self._lock:
                await self._flush()

    async def finish(
        self,
        turns: Sequence[HarnessTurn] = (),
        *,
        interrupted: bool = False,
        error: str | None = None,
    ) -> None:
        async with self._lock:
            now = self._clock.now()
            self._close_text(now, complete=not interrupted and error is None)
            for code_item, output_item, code_ms, _ in self._tools.values():
                self._revise(
                    code_item,
                    code_item.payload,
                    code_elapsed_ms=code_ms,
                    activity_pending=False,
                )
                self._revise(output_item, "Tool result unavailable", activity_pending=False)
            self._tools.clear()
            if not self._has_assistant_text and not interrupted:
                for turn in turns:
                    if turn.text:
                        self._new_chat(turn.text, now)
                        self._check_visibility(turn.text)
            if error is not None:
                self._new_item("system_marker", f"error:Harness execution stopped: {error}", now)
            self._has_assistant_text = False
            self._last_chat_raw = ""
            await self._flush()

    async def _on_late_reasoning(
        self, event: ReasoningDelta | ReasoningText | TextDelta | AssistantText, now: datetime
    ) -> bool:
        if (
            isinstance(event, ReasoningText)
            and event.text
            and self._text_item is not None
            and self._text_item.kind == "agent_chat"
        ):
            self._new_item(
                "agent_reasoning",
                event.text,
                now,
                reasoning_ms=_elapsed(self._text_started, now),
                activity_pending=False,
            )
            self._reasoning_ended = now
            await self._flush()
            return True
        return False

    def _is_duplicate_assistant_text(self, event: BridgeEvent, *, started: bool) -> bool:
        return bool(
            started
            and isinstance(event, AssistantText)
            and self._last_chat_raw
            and (
                self._last_chat_raw == event.text
                or self._last_chat_raw.strip() == (event.text or "").strip()
            )
        )

    def _step_text_item(
        self, current: TimelineItem | None, *, reasoning: bool, complete: bool, now: datetime
    ) -> TimelineItem:
        if current is None:
            self._text_started = self._activity_started
            return (
                self._new_item("agent_reasoning", self._text, now)
                if reasoning
                else self._new_chat(self._text, now, pending=not complete)
            )
        if reasoning:
            return self._revise(current, self._text)
        payload, user_facing = self._reply(self._text)
        return self._revise(current, payload, user_facing=user_facing)

    async def _on_text(
        self, event: ReasoningDelta | ReasoningText | TextDelta | AssistantText, now: datetime
    ) -> None:
        if await self._on_late_reasoning(event, now):
            return
        reasoning = isinstance(event, (ReasoningDelta, ReasoningText))
        complete = isinstance(event, (ReasoningText, AssistantText))
        kind = "agent_reasoning" if reasoning else "agent_chat"
        if self._text_item is not None and self._text_item.kind != kind:
            self._close_text(now)
        started = self._text_item is None
        if (started and not event.text) or self._is_duplicate_assistant_text(
            event, started=started
        ):
            return
        self._text = (event.text or self._text) if complete else self._text + event.text
        self._text_item = self._step_text_item(
            self._text_item, reasoning=reasoning, complete=complete, now=now
        )
        self._has_assistant_text |= not reasoning
        if complete:
            self._close_text(now)
        if started or complete:
            await self._flush()

    # meiva:note
    # A chat item is sealed whenever its text closes, complete or not: a half-written reply cut off by
    # an interrupt or an error is still on the timeline, and an item left pending is dropped from the
    # live merge and never reaches the delivery capture. An incomplete text skips the visibility
    # check and the duplicate-text guard, which judge a whole reply.
    def _close_text(self, now: datetime, *, complete: bool = True) -> None:
        if self._text_item is not None and self._text_item.kind == "agent_reasoning":
            self._revise(
                self._text_item, self._text, reasoning_ms=_elapsed(self._text_started, now)
            )
            self._reasoning_ended = now
        elif self._text_item is not None:
            if complete:
                self._check_visibility(self._text)
            if self._text_item.activity_pending:
                self._revise(self._text_item, self._text_item.payload, activity_pending=False)
            if complete:
                self._last_chat_raw = self._text
        self._text_item = None
        self._text = ""

    # meiva:note
    # The `agent_code` item of the call that started a sub-agent: one this projector wrote, else one an
    # earlier graph invocation wrote, found by the CLI's call id (a background sub-agent outlives the
    # invocation that saw its spawning call). A call found nowhere stays None, and is not asked again.
    # meiva:link base/lm/harness/docs/subagent-text.md
    async def _call_item(self, tool_use_id: str) -> str | None:
        if (known := self._tool_items.get(tool_use_id)) is not None:
            return known
        if self._find_call is None or tool_use_id in self._unfound_calls:
            return None
        found = await self._find_call(tool_use_id)
        if found is None:
            self._unfound_calls.add(tool_use_id)
            # meiva:note
            # Only the nesting is lost (the call's entry was never written to this agent's timeline, or
            # the timeline was reset since): the sub-agent's line is still written, as a top-level row, and
            # the gap is recorded once per call id, never failing a turn for a display detail.
            # meiva:link base/lm/harness/docs/subagent-text.md
            logger.warning(
                "sub-agent activity names a call with no timeline entry; shown without a parent",
                event="harness_subagent_call_not_found",
                tool_use_id=tool_use_id,
            )
        else:
            self._tool_items[tool_use_id] = found
        return found

    @staticmethod
    def _cut_subagent(text: str) -> str:
        return (
            text[:SUBAGENT_ITEM_CHARS] + SUBAGENT_CUT if len(text) > SUBAGENT_ITEM_CHARS else text
        )

    async def _on_tool_use(self, event: ToolUse, now: datetime) -> None:
        if event.tool_use_id in self._seen_tools:
            return
        self._seen_tools.add(event.tool_use_id)
        self._close_text(now)
        self._last_chat_raw = ""
        subagent = event.parent_tool_use_id is not None
        payload = format_tool_use(event.name, event.tool_input)
        parent = None
        if event.parent_tool_use_id is not None:
            parent = await self._call_item(event.parent_tool_use_id)
            payload = self._cut_subagent(payload)
        code_ms = 0 if subagent else _elapsed(self._reasoning_ended or self._activity_started, now)
        code_item = self._new_item(
            "agent_code",
            payload,
            now,
            source=event.name if event.name != "execute_code" else None,
            activity_pending=True,
            sdk_calls=[SdkCall(method=event.name, count=1)],
            parent_item_id=parent,
            tool_use_id=event.tool_use_id,
        )
        output_item = self._new_item(
            "code_output", "Tool running", now, activity_pending=True, parent_item_id=parent
        )
        self._tool_items[event.tool_use_id] = code_item.item_id
        self._tools[event.tool_use_id] = (code_item, output_item, code_ms, now)
        if not subagent:
            self._activity_started = now
            self._reasoning_ended = None

    async def _on_subagent_text(self, event: SubagentText, now: datetime) -> None:
        if not event.text.strip():
            return
        self._close_text(now)
        self._last_chat_raw = ""
        _ = self._new_item(
            "subagent_text",
            self._cut_subagent(event.text),
            now,
            source=event.agent,
            parent_item_id=await self._call_item(event.tool_use_id),
            user_facing=False,
        )

    def _on_tool_result(self, event: ToolResult, now: datetime) -> None:
        if (slot := self._tools.pop(event.tool_use_id, None)) is None:
            return
        code_item, output_item, code_ms, started = slot
        self._revise(
            code_item,
            code_item.payload,
            code_elapsed_ms=code_ms,
            activity_pending=False,
        )
        output = event.content or ("Tool failed" if event.is_error else "")
        subagent = event.parent_tool_use_id is not None
        if subagent:
            output = self._cut_subagent(output)
        self._revise(
            output_item,
            output,
            exec_ms=_elapsed(started, now),
            activity_pending=False,
            created_at=now.isoformat(),
        )
        if not subagent:
            self._activity_started = now

    def _check_visibility(self, text: str) -> None:
        if self._explicit_visibility and text.strip() and parse_reply_visibility(text) is None:
            self.invalid_reply_visibility = True

    def _reply(self, text: str) -> tuple[str, bool]:
        return resolve_agent_reply(
            text,
            non_human_wake=self._non_human_wake,
            explicit_visibility=self._explicit_visibility,
            delivery_mode=self._delivery_mode,
        )

    def _new_chat(self, text: str, now: datetime, *, pending: bool = False) -> TimelineItem:
        payload, user_facing = self._reply(text)
        return self._new_item(
            "agent_chat", payload, now, user_facing=user_facing, activity_pending=pending
        )

    def _new_item(self, kind: str, payload: str, now: datetime, **fields: object) -> TimelineItem:
        item = TimelineItem.model_validate(
            {
                **fields,
                "item_id": f"h{self._next_idx}.0",
                "kind": kind,
                "payload": payload,
                "created_at": now.isoformat(),
                "activity_revision": 1,
            }
        )
        self._next_idx += 1
        return self._queue(item)

    def _revise(self, item: TimelineItem, payload: str, **fields: object) -> TimelineItem:
        revision = item.activity_revision
        if revision is None:
            raise ValueError("harness activity revision is missing")
        return self._queue(
            item.model_copy(
                update={**fields, "payload": payload, "activity_revision": revision + 1}
            )
        )

    def _queue(self, item: TimelineItem) -> TimelineItem:
        self._pending[item.item_id] = item
        self._dirty.set()
        return item

    async def _flush(self) -> None:
        for item_id, item in list(self._pending.items()):
            msg_count = max(self._committed_count, int(item_id.split(".")[0].removeprefix("h")) + 1)
            await self._checkpoint(item, msg_count)
            self._committed_count = msg_count
            del self._pending[item_id]
        self._dirty.clear()


# meiva:note
# Usage is logged where every other agent turn logs it, with the harness name as the
# `source` so the two runtimes stay comparable. A turn whose provider reported no
# tokens is not logged at all: an all-zero row would read as a free call rather than
# as a provider that said nothing.
# meiva:link agent/graph/docs/harness-turn.md
def _resolve_pricing_model(raw_model: str, catalog: ModelCatalog) -> str:
    candidate = raw_model.removesuffix("[1m]")
    for suffix in ("", "-high", "-medium", "-low", "-minimal"):
        if candidate.endswith(suffix) and (s := candidate.removesuffix(suffix)) in catalog.prices:
            return raw_model if not suffix and raw_model in catalog.prices else s
    return raw_model


def log_turn_usage(ctx: MeivaContext, agent_id: int, model: str | None, turn: HarnessTurn) -> None:
    usage = turn.usage
    if usage.input_tokens == 0 and usage.output_tokens == 0:
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
            log_usage_fields(
                pricing_model,
                fields,
                usage_kind="agent",
                catalog=catalog,
                now=ctx.require_clock().now(),
            )
    except Exception as error:
        # meiva:note
        # A bill that cannot be priced is a hole in an accounting row; the reply it
        # describes is real either way. The turn has already finished by the time it
        # is logged, so a raise here deletes a reply the user paid for — a provider
        # that reports token counts in a shape the price book rejects must not cost
        # the answer, and the recorded warning keeps the gap visible.
        # meiva:link agent/graph/docs/harness-turn.md
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
# The projector that writes an external harness's activity to the agent's timeline: each item is
# checkpointed under one position and the calls an earlier invocation wrote are found by the CLI's call id.
# meiva:link agent/graph/docs/harness-turn.md
async def activity_projector(
    ctx: MeivaContext,
    agent_id: int,
    publisher: AgentEventPublisher,
    *,
    non_human_wake: bool,
) -> HarnessTurnProjector:
    base_idx = 0
    if ctx.ops_pool is not None:
        async with ctx.ops_pool.connection() as pool_conn:
            base_idx = await aget_next_harness_message_seq(pool_conn, agent_id)

    async def checkpoint(item: TimelineItem, msg_count: int) -> None:
        await _checkpoint_activity(ctx, agent_id, publisher, item, msg_count)
        await capture_items(ctx, agent_id, [item], source_key="harness")

    async def find_call(tool_use_id: str) -> str | None:
        if ctx.ops_pool is None:
            return None
        async with ctx.ops_pool.connection() as pool_conn:
            return await aget_harness_call_item_id(pool_conn, agent_id, tool_use_id)

    return HarnessTurnProjector(
        checkpoint=checkpoint,
        find_call=find_call,
        base_idx=base_idx,
        clock=ctx.require_clock(),
        non_human_wake=non_human_wake,
        explicit_visibility=ctx.require_agent().prompt.user_delivery_mode == "prefix",
        delivery_mode=ctx.require_agent().prompt.user_delivery_mode,
    )


# meiva:note
# What a CLI sub-agent did while no turn ran (Codex's child thread after its parent's turn ended) is
# written to the timeline under the call that started it, and that is all: it is not a run. No
# before-turn hook runs, so no hook note becomes an input, and nothing is delivered to the CLI,
# because the sub-agent's work is not a reason to start a model turn. Inputs left in the inbox wait for
# the claim's ordinary wake.
# The events are read, written and only then acknowledged, one by one: this is display data, so a write
# that fails is recorded (`harness_subagent_activity_unwritten`) and never fails the claim, and what was
# not written stays for the next wake (which the pump spaces by `ORPHAN_REWAKE_SECONDS`).
# meiva:link base/lm/harness/docs/subagent-text.md
async def present_unattached(ctx: MeivaContext, agent_id: int) -> None:
    bridge = ctx.require_harness_bridge()
    events = bridge.unattached()
    if not events:
        return
    publisher = ctx.event_publisher
    assert publisher is not None, "presenting sub-agent activity requires ctx.event_publisher"  # noqa: S101
    written = 0
    try:
        projector = await activity_projector(ctx, agent_id, publisher, non_human_wake=True)
        for event in events:
            await projector.on_event(event)
            bridge.acknowledge_unattached(1)
            written += 1
        await projector.finish()
    except Exception as error:
        logger.warning(
            "sub-agent activity could not be written to the timeline; {kept} event(s) kept for the next wake",
            event="harness_subagent_activity_unwritten",
            agent_id=agent_id,
            kept=len(events) - written,
            error=repr(error),
        )
