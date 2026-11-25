from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from base.agents.history.transcript_timeline.project import ProjectedItem
from base.api_contracts.user_delivery import UserDeliveryMode
from base.events.live.coalescer import DeltaCoalescer
from base.events.live.projection import (
    ChatDelta,
    ChatStart,
    ReasoningDelta,
    ReasoningStart,
)
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    ReasoningText,
    ToolUse,
)
from base.lm.harness import (
    ReasoningDelta as HarnessReasoningDelta,
)
from base.lm.harness import (
    TextDelta as HarnessTextDelta,
)

LIVE_PREFIX = "live."

Kind = Literal["chat", "reasoning"]

_COMMITTED_KINDS: dict[str, Kind] = {"agent_chat": "chat", "agent_reasoning": "reasoning"}


@dataclass(slots=True)
class _Block:
    item_id: str
    turn_id: str
    kind: Kind
    key: str | None
    parts: list[str]

    @property
    def text(self) -> str:
        return "".join(self.parts)


class LiveOverlay:
    def __init__(
        self,
        emit: Callable[[str], None],
        agent_id: int,
        *,
        non_human_wake: Callable[[], bool],
        explicit_visibility: bool,
        delivery_mode: UserDeliveryMode | None,
    ) -> None:
        self._emit = emit
        self._agent_id = agent_id
        self._non_human_wake = non_human_wake
        self._explicit_visibility = explicit_visibility
        self._delivery_mode = delivery_mode
        self._coalescer = DeltaCoalescer(self._emit_delta)
        self._blocks: dict[str, _Block] = {}
        self._open_unkeyed: dict[Kind, str] = {}
        self._ordinal = 0
        self._closed: set[str] = set()
        self._committed_keys: set[str] = set()
        self._ended_turns: set[str] = set()

    @property
    def live_ids(self) -> list[str]:
        return list(self._blocks)

    def on_event(self, event: BridgeEvent) -> None:
        if isinstance(event, HarnessTextDelta):
            self._delta(event.turn_id, "chat", event.block, event.text)
        elif isinstance(event, HarnessReasoningDelta):
            self._delta(event.turn_id, "reasoning", event.block, event.text)
        elif isinstance(event, AssistantText):
            _ = self._open_unkeyed.pop("chat", None)
        elif isinstance(event, ReasoningText):
            _ = self._open_unkeyed.pop("reasoning", None)
        elif isinstance(event, ToolUse):
            self._open_unkeyed.clear()

    def _delta(self, turn_id: str, kind: Kind, key: str | None, text: str) -> None:
        if not text or turn_id in self._ended_turns or key in self._committed_keys:
            return
        item_id = self._item_id(turn_id, kind, key)
        if item_id in self._closed:
            return
        block = self._blocks.get(item_id)
        if block is None:
            block = _Block(item_id=item_id, turn_id=turn_id, kind=kind, key=key, parts=[])
            self._blocks[item_id] = block
            self._emit_start(block)
        block.parts.append(text)
        self._coalescer.append(f"{kind}|{item_id}", text)

    def _item_id(self, turn_id: str, kind: Kind, key: str | None) -> str:
        if key is not None:
            return f"{LIVE_PREFIX}{turn_id}.{key}"
        item_id = self._open_unkeyed.get(kind)
        if item_id is None:
            item_id = f"{LIVE_PREFIX}{turn_id}.n{self._ordinal}"
            self._ordinal += 1
            self._open_unkeyed[kind] = item_id
        return item_id

    def superseded(self, committed: Sequence[ProjectedItem]) -> list[str]:
        retired: list[str] = []
        for item in committed:
            kind = _COMMITTED_KINDS.get(item.item.kind)
            if kind is None:
                continue
            if item.block is not None:
                self._committed_keys.add(item.block)
            retired.extend(
                block.item_id
                for block in self._blocks.values()
                if block.kind == kind and block.item_id not in retired and _matches(block, item)
            )
        return self._retire(retired)

    def retire_all(self) -> list[str]:
        self._ended_turns.update(block.turn_id for block in self._blocks.values())
        return self._retire(list(self._blocks))

    def _retire(self, item_ids: list[str]) -> list[str]:
        self._coalescer.flush()
        for item_id in item_ids:
            del self._blocks[item_id]
            self._closed.add(item_id)
            self._open_unkeyed = {
                kind: open_id for kind, open_id in self._open_unkeyed.items() if open_id != item_id
            }
        return item_ids

    def _emit_start(self, block: _Block) -> None:
        if block.kind == "chat":
            self._emit(
                ChatStart(
                    agent_id=self._agent_id,
                    item_id=block.item_id,
                    non_human_wake=self._non_human_wake(),
                    explicit_visibility=self._explicit_visibility,
                    delivery_mode=self._delivery_mode,
                ).model_dump_json()
            )
        else:
            self._emit(
                ReasoningStart(agent_id=self._agent_id, item_id=block.item_id).model_dump_json()
            )

    def _emit_delta(self, key: str, content: str) -> None:
        kind, item_id = key.split("|", 1)
        if kind == "chat":
            self._emit(
                ChatDelta(
                    agent_id=self._agent_id,
                    item_id=item_id,
                    content=content,
                    non_human_wake=self._non_human_wake(),
                    explicit_visibility=self._explicit_visibility,
                    delivery_mode=self._delivery_mode,
                ).model_dump_json()
            )
        else:
            self._emit(
                ReasoningDelta(
                    agent_id=self._agent_id, item_id=item_id, content=content
                ).model_dump_json()
            )


def _matches(block: _Block, item: ProjectedItem) -> bool:
    if block.key is not None and item.block is not None:
        return block.key == item.block
    live = block.text.strip()
    return bool(live) and item.text.strip().startswith(live)
