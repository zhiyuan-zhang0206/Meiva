from __future__ import annotations

from dataclasses import dataclass

from base.host.boundary.json_values import is_object_list
from base.lm.harness.codex.frames import RequestParams, as_object, as_text
from base.lm.harness.events import SubagentText


# meiva:note
# A Codex sub-agent is a thread of its own on the same app-server connection. The parent thread's
# `spawnAgent` call names it once, in the completed item's `receiverThreadIds`; every later
# notification of that thread (recorded on CLI 0.166: `item/completed` with the child's `threadId`)
# carries only the child id. So the child thread is mapped to the spawn call's item id when the call
# completes, and a child's finished `agentMessage` items are looked up through that map. A child the
# map does not know is not ours to show. Codex names no sub-agent label on these frames.
# meiva:link base/lm/harness/docs/subagent-text.md
@dataclass(frozen=True, slots=True)
class SubagentThreads:
    calls: tuple[tuple[str, str], ...] = ()

    def learned(
        self, method: str, params: RequestParams, *, session_id: str | None
    ) -> SubagentThreads:
        if method != "item/completed" or session_id is None or params.get("threadId") != session_id:
            return self
        item = as_object(params.get("item"))
        call_id = as_text(item.get("id"))
        threads = item.get("receiverThreadIds")
        if (
            item.get("type") != "collabAgentToolCall"
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
        return SubagentThreads((*self.calls, *added)) if added else self

    def text(self, method: str, params: RequestParams, *, turn_id: str) -> SubagentText | None:
        thread = as_text(params.get("threadId"))
        call_id = next((call for child, call in self.calls if child == thread), None)
        if method != "item/completed" or call_id is None:
            return None
        item = as_object(params.get("item"))
        text = as_text(item.get("text"))
        if item.get("type") != "agentMessage" or not text:
            return None
        return SubagentText(turn_id=turn_id, tool_use_id=call_id, agent=None, text=text)
