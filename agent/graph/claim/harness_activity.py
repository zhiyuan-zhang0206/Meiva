from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Protocol

from base.agents.context import MeivaContext
from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import format_tool_use
from base.agents.messages.reply_intent import parse_reply_visibility, resolve_agent_reply
from base.agents.sdk.telemetry import SdkCall
from base.clock import Clock
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

    def take_invalid_reply(self) -> bool:
        invalid, self.invalid_reply_visibility = self.invalid_reply_visibility, False
        return invalid

    def mark_human(self) -> None:
        self._non_human_wake = False

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
        input_keys: Sequence[str] = (),
    ) -> None:
        del input_keys
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
def resolve_pricing_model(raw_model: str, catalog: ModelCatalog) -> str:
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
    pricing_model = resolve_pricing_model(model or ctx.require_agent().brain.llm_model, catalog)
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


# meiva:note
# What the foreground run, the interrupt plumbing and the reconciliation helpers ask of "whatever shows the turn
# on the timeline". Two things answer it. `HarnessTurnProjector` (the adapter-event path) builds items from the
# events and writes them. `ReadTimelineSink` (`harness_read_sink.py`, for a harness whose timeline is the CLI's own
# record) writes nothing: the timeline is read from the record, so the sink only carries the few acts the invocation
# still owns: delivering the reply and the wake polarity.
# meiva:link agent/graph/docs/harness-turn.md
class TurnSink(Protocol):
    @property
    def has_activity(self) -> bool: ...

    @property
    def non_human_wake(self) -> bool: ...

    def take_invalid_reply(self) -> bool: ...

    def mark_human(self) -> None: ...

    async def on_event(self, event: BridgeEvent) -> None: ...

    async def record_control(self, text: str, *, error: bool = False) -> None: ...

    async def append_inbound(
        self,
        commit: Callable[[], Awaitable[list[TimelineItem]]],
        *,
        human_input: bool = False,
    ) -> list[TimelineItem]: ...

    async def flush_text(self) -> None: ...

    async def finish(
        self,
        turns: Sequence[HarnessTurn] = (),
        *,
        interrupted: bool = False,
        error: str | None = None,
        input_keys: Sequence[str] = (),
    ) -> None: ...
