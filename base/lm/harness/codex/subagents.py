from __future__ import annotations

from dataclasses import dataclass, replace

from base.host.boundary.json_values import is_object_list
from base.lm.harness.codex.frames import RequestParams, as_object, as_text
from base.lm.harness.codex.mapping import item_event
from base.lm.harness.events import NO_TURN, BridgeEvent, SubagentText, ToolResult, ToolUse
from base.lm.harness.session import BackgroundItem, BackgroundKind, describe

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
# A child thread is its own line of work: it keeps going after the parent's turn ended, and what it
# says then is shown under its spawn call with no turn (`NO_TURN`); nothing is invented for it. A tool
# item seen with no turn is shown when it completes, as a call followed by its result, because the run
# that shows it may end before a later completion would arrive.
# A child's failure is shown too: an `error` that will not be retried, and a `turn/completed` that is
# failed or interrupted, become a line under the spawn call. The parent's own `collabAgentToolCall`
# result carries `agentsStates` (`errored` with a message) only for a call that waits on the child, so
# it cannot be relied on.
# A child that is working holds the session: it lives in the app-server, so closing the app-server
# would end it. It counts from the spawn call that named it (unless that call already reports it
# finished) until it goes idle or fails: its own `turn/completed`, a `thread/status/changed` that is
# not `active`, an `error` that will not be retried, or the parent's `closeAgent` call. A child's
# `turn/started` or an `active` status reopens it, because a finished child can be given more work.
# meiva:link base/lm/harness/docs/subagent-text.md
_FINISHED = frozenset({"completed", "errored", "shutdown", "interrupted", "notFound"})
_LABEL_LIMIT = 100


@dataclass(frozen=True, slots=True)
class SubagentThreads:
    calls: tuple[tuple[str, str], ...] = ()
    started: frozenset[str] = frozenset()
    labels: tuple[tuple[str, BackgroundItem], ...] = ()
    working: frozenset[str] = frozenset()

    @property
    def background(self) -> tuple[BackgroundItem, ...]:
        by_thread = dict(self.labels)
        return tuple(by_thread[thread] for thread in sorted(self.working) if thread in by_thread)

    def learned(
        self, method: str, params: RequestParams, *, session_id: str | None
    ) -> SubagentThreads:
        if method == "item/started":
            return self._with_started(params)
        if method == "item/completed" and session_id is not None:
            return self._with_spawned(params, session_id)._with_closed(params, session_id)
        return self._with_activity(method, params)

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
        return self._adopting(added, item) if added else self

    def _adopting(self, added: tuple[tuple[str, str], ...], item: RequestParams) -> SubagentThreads:
        label = describe(as_text(item.get("prompt")) or "sub-agent", limit=_LABEL_LIMIT)
        states = as_object(item.get("agentsStates"))
        return replace(
            self,
            calls=(*self.calls, *added),
            labels=(
                *self.labels,
                *((thread, BackgroundItem(BackgroundKind.AGENT, label)) for thread, _ in added),
            ),
            working=self.working
            | {
                thread
                for thread, _ in added
                if as_text(as_object(states.get(thread)).get("status")) not in _FINISHED
            },
        )

    def _with_closed(self, params: RequestParams, session_id: str) -> SubagentThreads:
        item = as_object(params.get("item"))
        threads = item.get("receiverThreadIds")
        if (
            params.get("threadId") != session_id
            or item.get("tool") != "closeAgent"
            or not is_object_list(threads)
        ):
            return self
        return replace(self, working=self.working - {t for t in threads if isinstance(t, str)})

    def _with_activity(self, method: str, params: RequestParams) -> SubagentThreads:
        thread = as_text(params.get("threadId"))
        if thread is None or self._call_of(params) is None:
            return self
        if method == "turn/started":
            return replace(self, working=self.working | {thread})
        if (
            method == "turn/completed"
            and as_object(params.get("turn")).get("status") != "inProgress"
        ):
            return replace(self, working=self.working - {thread})
        if method == "error" and not bool(params.get("willRetry", False)):
            return replace(self, working=self.working - {thread})
        if method == "thread/status/changed":
            active = as_text(as_object(params.get("status")).get("type")) == "active"
            return replace(
                self, working=self.working | {thread} if active else self.working - {thread}
            )
        return self

    def _call_of(self, params: RequestParams) -> str | None:
        thread = as_text(params.get("threadId"))
        return next((call for child, call in self.calls if child == thread), None)

    def tools(self, method: str, params: RequestParams, *, turn_id: str) -> list[BridgeEvent]:
        call_id = self._call_of(params)
        if call_id is None or method not in {"item/started", "item/completed"}:
            return []
        completed = method == "item/completed"
        if turn_id == NO_TURN and not completed:
            return []
        events = item_event(params, phase="completed" if completed else "started", turn_id=turn_id)
        unseen = as_text(as_object(params.get("item")).get("id")) not in self.started
        if completed and (unseen or turn_id == NO_TURN):
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

    def failure(self, method: str, params: RequestParams, *, turn_id: str) -> SubagentText | None:
        call_id = self._call_of(params)
        if call_id is None:
            return None
        if method == "error" and not bool(params.get("willRetry", False)):
            message = as_text(as_object(params.get("error")).get("message")) or "no message"
            return SubagentText(turn_id, call_id, None, f"Sub-agent error: {message}")
        turn = as_object(params.get("turn"))
        status = as_text(turn.get("status"))
        if method == "turn/completed" and status in {"failed", "interrupted"}:
            message = as_text(as_object(turn.get("error")).get("message"))
            detail = f": {message}" if message else ""
            return SubagentText(turn_id, call_id, None, f"Sub-agent turn {status}{detail}")
        return None
