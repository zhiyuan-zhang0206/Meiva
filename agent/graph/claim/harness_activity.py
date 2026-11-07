from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from agent.graph.claim.harness_tool_format import format_tool_use
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.reply_intent import parse_reply_visibility, resolve_agent_reply
from base.agents.sdk.telemetry import SdkCall
from base.clock import Clock
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    ReasoningDelta,
    ReasoningText,
    TextDelta,
    ToolResult,
    ToolUse,
)
from base.lm.harness.engine import HarnessTurn

TEXT_CHECKPOINT_SECONDS = 0.2


def _elapsed(start: datetime, end: datetime) -> int:
    return max(int((end - start).total_seconds() * 1000), 0)


# meiva:note
# Checkpoint each observed activity under one stable timeline position.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
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
        self._tools: dict[str, tuple[TimelineItem, datetime]] = {}
        self._seen_tools: set[str] = set()
        self._pending: dict[str, TimelineItem] = {}
        self._dirty = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def has_activity(self) -> bool:
        return self._next_idx > self._base_idx

    async def on_event(self, event: BridgeEvent) -> None:
        async with self._lock:
            now = self._clock.now()
            match event:
                case ReasoningDelta() | ReasoningText() | TextDelta() | AssistantText():
                    await self._on_text(event, now)
                case ToolUse():
                    self._on_tool_use(event, now)
                    await self._flush()
                case ToolResult():
                    self._on_tool_result(event, now)
                    await self._flush()
                case _:
                    raise TypeError(f"unexpected harness activity: {type(event).__name__}")

    async def record_control(self, text: str, *, error: bool = False) -> None:
        async with self._lock:
            prefix = "error:" if error else "status:"
            self._new_item("system_marker", f"{prefix}{text}", self._clock.now())
            await self._flush()

    async def append_inbound(
        self, commit: Callable[[], Awaitable[list[TimelineItem]]]
    ) -> list[TimelineItem]:
        async with self._lock:
            await self._flush()
            items = await commit()
            for item in items:
                if item.item_id not in (f"h{self._next_idx}.0", f"{self._next_idx}.0"):
                    raise RuntimeError(f"unexpected harness inbound position {item.item_id}")
                self._next_idx += 1
            self._committed_count = self._next_idx
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
            self._close_text(now, seal_chat=not interrupted and error is None)
            for item, _ in self._tools.values():
                self._revise(item, "Tool result unavailable")
            self._tools.clear()
            if not self._has_assistant_text and not interrupted:
                for turn in turns:
                    if turn.text and not turn.replayed:
                        self._new_chat(turn.text, now)
                        self._check_visibility(turn.text)
            if error is not None:
                self._new_item("system_marker", f"error:Harness execution stopped: {error}", now)
            await self._flush()

    async def _on_text(
        self, event: ReasoningDelta | ReasoningText | TextDelta | AssistantText, now: datetime
    ) -> None:
        reasoning = isinstance(event, (ReasoningDelta, ReasoningText))
        complete = isinstance(event, (ReasoningText, AssistantText))
        kind = "agent_reasoning" if reasoning else "agent_chat"
        if self._text_item is not None and self._text_item.kind != kind:
            self._close_text(now)
        started = self._text_item is None
        if started and not event.text:
            return
        self._text = (event.text or self._text) if complete else self._text + event.text
        current = self._text_item
        if current is None:
            self._text_started = self._activity_started
            self._text_item = (
                self._new_item("agent_reasoning", self._text, now)
                if reasoning
                else self._new_chat(self._text, now, pending=not complete)
            )
        elif reasoning:
            self._text_item = self._revise(current, self._text)
        else:
            payload, user_facing = self._reply(self._text)
            self._text_item = self._revise(current, payload, user_facing=user_facing)
        self._has_assistant_text |= not reasoning
        if complete:
            self._close_text(now)
        if started or complete:
            await self._flush()

    def _close_text(self, now: datetime, *, seal_chat: bool = True) -> None:
        if self._text_item is not None and self._text_item.kind == "agent_reasoning":
            self._revise(
                self._text_item, self._text, reasoning_ms=_elapsed(self._text_started, now)
            )
            self._reasoning_ended = now
        elif self._text_item is not None and seal_chat:
            self._check_visibility(self._text)
            if self._text_item.activity_pending:
                self._revise(self._text_item, self._text_item.payload, activity_pending=False)
        self._text_item = None
        self._text = ""

    def _on_tool_use(self, event: ToolUse, now: datetime) -> None:
        if event.tool_use_id in self._seen_tools:
            return
        self._seen_tools.add(event.tool_use_id)
        self._close_text(now)
        code_ms = _elapsed(self._reasoning_ended or self._activity_started, now)
        self._new_item(
            "agent_code",
            format_tool_use(event.name, event.tool_input),
            now,
            source=event.name if event.name != "execute_code" else None,
            code_elapsed_ms=code_ms,
            sdk_calls=[SdkCall(method=event.name, count=1)],
        )
        self._tools[event.tool_use_id] = (self._new_item("code_output", "Tool running", now), now)
        self._activity_started = now
        self._reasoning_ended = None

    def _on_tool_result(self, event: ToolResult, now: datetime) -> None:
        if (slot := self._tools.pop(event.tool_use_id, None)) is None:
            return
        item, started = slot
        self._revise(
            item,
            event.content or ("Tool failed" if event.is_error else ""),
            exec_ms=_elapsed(started, now),
            created_at=now.isoformat(),
        )
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
