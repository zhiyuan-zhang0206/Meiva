from __future__ import annotations

from dataclasses import dataclass, replace

from base.host.boundary.json_values import is_object_list
from base.lm.harness.codex.frames import RequestParams, as_object, as_text
from base.lm.harness.codex.mapping import item_event
from base.lm.harness.events import BridgeEvent, SubagentText, ToolResult, ToolUse


# meiva:note
# A Codex sub-agent is a thread of its own on the same app-server connection. The parent thread's
# `spawnAgent` call names it once, in the completed item's `receiverThreadIds`; every later
# notification of that thread (recorded on CLI 0.166: `item/completed` with the child's `threadId`)
# carries only the child id. So the child thread is mapped to the spawn call's item id when the call
# completes, and a child's finished `agentMessage` items are looked up through that map. A child the
# map does not know is not ours to show. Codex names no sub-agent label on these frames.
# The child's tool items (`commandExecution`, `mcpToolCall`, ...) are the same item shapes as the
# parent's and are mapped by the same function, then attributed to the spawn call. The recorded child
# thread sends a tool item as `item/completed` alone, with no `item/started` before it, so a completed
# item the child never started is shown as a call followed by its result.
# meiva:link base/lm/harness/docs/subagent-text.md
@dataclass(frozen=True, slots=True)
class SubagentThreads:
    calls: tuple[tuple[str, str], ...] = ()
    started: frozenset[str] = frozenset()

    def learned(
        self, method: str, params: RequestParams, *, session_id: str | None
    ) -> SubagentThreads:
        if method == "item/started":
            return self._with_started(params)
        if method == "item/completed" and session_id is not None:
            return self._with_spawned(params, session_id)
        return self

    def _with_started(self, params: RequestParams) -> SubagentThreads:
        item_id = as_text(as_object(params.get("item")).get("id"))
        if item_id is None or self._call_of(params) is None:
            return self
        return replace(self, started=self.started | {item_id})

    def _with_spawned(self, params: RequestParams, session_id: str) -> SubagentThreads:
        item = as_object(params.get("item"))
        call_id = as_text(item.get("id"))
        threads = item.get("receiverThreadIds")
        if (
            params.get("threadId") != session_id
            or item.get("type") != "collabAgentToolCall"
            or item.get("tool") != "spawnAgent"
            or not call_id
            or not is_object_list(threads)
        ):
            return self
        known = {thread for thread, _ in self.calls}
        added = tuple(
            (thread, call_id)
            for thread in threads
            if isinstance(thread, str) and thread not in known
        )
        return replace(self, calls=(*self.calls, *added)) if added else self

    def _call_of(self, params: RequestParams) -> str | None:
        thread = as_text(params.get("threadId"))
        return next((call for child, call in self.calls if child == thread), None)

    def tools(self, method: str, params: RequestParams, *, turn_id: str) -> list[BridgeEvent]:
        call_id = self._call_of(params)
        if call_id is None or method not in {"item/started", "item/completed"}:
            return []
        completed = method == "item/completed"
        events = item_event(params, phase="completed" if completed else "started", turn_id=turn_id)
        if completed and as_text(as_object(params.get("item")).get("id")) not in self.started:
            events = [*item_event(params, phase="started", turn_id=turn_id), *events]
        return [
            replace(event, parent_tool_use_id=call_id)
            for event in events
            if isinstance(event, ToolUse | ToolResult)
        ]

    def text(self, method: str, params: RequestParams, *, turn_id: str) -> SubagentText | None:
        call_id = self._call_of(params)
        if method != "item/completed" or call_id is None:
            return None
        item = as_object(params.get("item"))
        text = as_text(item.get("text"))
        if item.get("type") != "agentMessage" or not text:
            return None
        return SubagentText(turn_id=turn_id, tool_use_id=call_id, agent=None, text=text)
