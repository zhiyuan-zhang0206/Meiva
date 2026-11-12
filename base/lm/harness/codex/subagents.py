from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from base.host.boundary.json_values import is_object_list
from base.lm.harness.codex.config import CHILD_SILENCE_SECONDS
from base.lm.harness.codex.frames import RequestParams, as_object, as_text
from base.lm.harness.codex.mapping import item_event
from base.lm.harness.events import BridgeEvent, SubagentText, ToolResult, ToolUse
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
# A child that is working holds the session: it lives in the app-server, so closing the app-server
# would end it. It counts from the spawn call that named it (unless that call already reports it
# finished, or its own end was seen before the call) until it goes idle or fails: its own
# `turn/completed`, a `thread/status/changed` that is not `active`, an `error` that will not be retried,
# or the parent's `closeAgent` call. A child's `turn/started` or an `active` status reopens it, because
# a finished child can be given more work. Every frame of a known child, whether or not it is shown,
# restarts the silence bound: a child that sent no frame for `CHILD_SILENCE_SECONDS` is taken as finished,
# so a child whose end never arrives cannot hold the session until the cache bound takes it.
# meiva:link base/lm/harness/docs/background-work.md
_FINISHED = frozenset({"completed", "errored", "shutdown", "interrupted", "notFound"})
_LABEL_LIMIT = 100
_ENDED_REMEMBERED = 64


@dataclass(frozen=True, slots=True)
class Child:
    thread: str
    label: BackgroundItem
    seen_at: float
    working: bool


@dataclass(frozen=True, slots=True)
class SubagentThreads:
    calls: tuple[tuple[str, str], ...] = ()
    started: frozenset[str] = frozenset()
    children: tuple[Child, ...] = ()
    # meiva:note
    # Threads the map does not know yet that already sent a frame saying they ended: a child that finished
    # before the parent's spawn call completed is never counted as working by that call.
    ended_early: tuple[str, ...] = ()

    def background(self, now: float) -> tuple[BackgroundItem, ...]:
        return tuple(
            child.label
            for child in sorted(self.children, key=lambda c: c.thread)
            if child.working and now - child.seen_at < CHILD_SILENCE_SECONDS
        )

    def learned(
        self, method: str, params: RequestParams, *, session_id: str | None, now: float
    ) -> SubagentThreads:
        state = self
        if method == "item/started":
            state = self._with_started(params)
        elif method == "item/completed" and session_id is not None:
            state = self._with_spawned(params, session_id, now)._with_closed(params, session_id)
        thread = as_text(params.get("threadId"))
        if thread is None or thread == session_id:
            return state
        if state._call_of(params) is None:
            return state._with_unknown(method, thread, params)
        return state._with_child_frame(method, thread, params, now)

    def _with_started(self, params: RequestParams) -> SubagentThreads:
        item_id = as_text(as_object(params.get("item")).get("id"))
        if item_id is None or self._call_of(params) is None:
            return self
        return replace(self, started=self.started | {item_id})

    def _with_spawned(self, params: RequestParams, session_id: str, now: float) -> SubagentThreads:
        item = as_object(params.get("item"))
        call_id = as_text(item.get("id"))
        threads = _receivers(params, item, session_id, tool="spawnAgent")
        known = {thread for thread, _ in self.calls}
        added = tuple(t for t in threads if t not in known)
        if not call_id or not added:
            return self
        label = BackgroundItem(
            BackgroundKind.AGENT,
            describe(as_text(item.get("prompt")) or "sub-agent", limit=_LABEL_LIMIT),
        )
        states = as_object(item.get("agentsStates"))
        adopted = tuple(
            Child(
                thread=thread,
                label=label,
                seen_at=now,
                working=thread not in self.ended_early
                and as_text(as_object(states.get(thread)).get("status")) not in _FINISHED,
            )
            for thread in added
        )
        return replace(
            self,
            calls=(*self.calls, *((thread, call_id) for thread in added)),
            children=(*self.children, *adopted),
            ended_early=tuple(t for t in self.ended_early if t not in added),
        )

    def _with_closed(self, params: RequestParams, session_id: str) -> SubagentThreads:
        item = as_object(params.get("item"))
        threads = _receivers(params, item, session_id, tool="closeAgent")
        return self._mapped(lambda c: replace(c, working=False) if c.thread in threads else c)

    def _mapped(self, change: Callable[[Child], Child]) -> SubagentThreads:
        return replace(self, children=tuple(change(child) for child in self.children))

    def _with_unknown(self, method: str, thread: str, params: RequestParams) -> SubagentThreads:
        ended = _ends(method, params)
        if ended is None:
            return self
        rest = tuple(t for t in self.ended_early if t != thread)
        if not ended:
            return replace(self, ended_early=rest)
        return replace(self, ended_early=(*rest, thread)[-_ENDED_REMEMBERED:])

    def _with_child_frame(
        self, method: str, thread: str, params: RequestParams, now: float
    ) -> SubagentThreads:
        ended = _ends(method, params)

        def change(child: Child) -> Child:
            if child.thread != thread:
                return child
            if ended is None:
                return replace(child, seen_at=now)
            return replace(child, seen_at=now, working=not ended)

        return self._mapped(change)

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


# meiva:note
# The threads a parent's `collabAgentToolCall` of the named tool names, or none when the item is not
# one of the parent's own.
def _receivers(
    params: RequestParams, item: RequestParams, session_id: str, *, tool: str
) -> tuple[str, ...]:
    threads = item.get("receiverThreadIds")
    if (
        params.get("threadId") != session_id
        or item.get("type") != "collabAgentToolCall"
        or item.get("tool") != tool
        or not is_object_list(threads)
    ):
        return ()
    return tuple(t for t in threads if isinstance(t, str))


# meiva:note
# What a child frame says about whether its thread is working: True for an end (a finished or failed
# turn, an error that will not be retried, a status that is not `active`), False for a start (`turn/started`
# or an `active` status), None for a frame that says nothing about it.
def _ends(method: str, params: RequestParams) -> bool | None:
    if method == "turn/started":
        return False
    if method == "turn/completed":
        return as_object(params.get("turn")).get("status") != "inProgress"
    if method == "error":
        return None if bool(params.get("willRetry", False)) else True
    if method == "thread/status/changed":
        return as_text(as_object(params.get("status")).get("type")) != "active"
    return None
