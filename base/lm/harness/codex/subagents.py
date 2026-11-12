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
# parent's and are mapped by the same function, then attributed to the spawn call. A tool item is shown
# when it completes, as a call followed by its result: the recorded child thread sends it as
# `item/completed` alone, and a call shown earlier could be left unfinished by a run that ends before the
# result arrives, so no call is ever written without its result.
# A child thread is its own line of work: it keeps going after the parent's turn ended, and what it
# says then is shown under its spawn call with no turn (`NO_TURN`); nothing is invented for it.
# A child's failure is shown too: an `error` that will not be retried, and a `turn/completed` that is
# failed or interrupted, become one line under the spawn call (the `turn/completed` that follows an
# `error` of the same turn adds none). The parent's own `collabAgentToolCall` result carries
# `agentsStates` (`errored` with a message) only for a call that waits on the child, so it cannot be
# relied on.
# A child that is working holds the session: it lives in the app-server, so closing the app-server
# would end it. It counts from the spawn call that named it (unless that call already reports it
# finished, or its own end was seen before the call) until it goes idle or fails: its own
# `turn/completed`, a `thread/status/changed` that is not `active`, an `error` that will not be retried,
# or the parent's `closeAgent` call. A child's `turn/started` or an `active` status reopens it, because
# a finished child can be given more work. The hold has an upper bound: a child that sent no frame for
# `CHILD_SILENCE_SECONDS` is taken as finished, so a child whose end never arrives cannot hold the
# session until the cache bound takes it.
# meiva:link base/lm/harness/docs/subagent-text.md
_FINISHED = frozenset({"completed", "errored", "shutdown", "interrupted", "notFound"})
_LABEL_LIMIT = 100
_ENDED_REMEMBERED = 64


@dataclass(frozen=True, slots=True)
class Child:
    thread: str
    call: str
    label: BackgroundItem
    seen_at: float
    working: bool
    reported_failure: bool = False


@dataclass(frozen=True, slots=True)
class SubagentThreads:
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
        if method == "item/completed" and session_id is not None:
            spawned = self._with_spawned(params, session_id, now)
            return spawned._with_closed(params, session_id)
        thread = as_text(params.get("threadId"))
        if thread is None:
            return self
        if self._call_of(params) is None:
            return self._with_unknown(method, thread, params)
        return self._with_child_frame(method, thread, params, now)

    def _with_spawned(self, params: RequestParams, session_id: str, now: float) -> SubagentThreads:
        item = as_object(params.get("item"))
        call_id = as_text(item.get("id"))
        threads = _receivers(params, item, session_id, tool="spawnAgent")
        known = {child.thread for child in self.children}
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
                call=call_id,
                label=label,
                seen_at=now,
                working=thread not in self.ended_early
                and as_text(as_object(states.get(thread)).get("status")) not in _FINISHED,
            )
            for thread in added
        )
        return replace(
            self,
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
        failed = method == "error" and ended is True

        def change(child: Child) -> Child:
            if child.thread != thread:
                return child
            if ended is None:
                return replace(child, seen_at=now)
            return replace(
                child,
                seen_at=now,
                working=not ended,
                reported_failure=failed or (child.reported_failure and method != "turn/started"),
            )

        return self._mapped(change)

    def _call_of(self, params: RequestParams) -> str | None:
        thread = as_text(params.get("threadId"))
        return next((child.call for child in self.children if child.thread == thread), None)

    def tools(self, method: str, params: RequestParams, *, turn_id: str) -> list[BridgeEvent]:
        call_id = self._call_of(params)
        if call_id is None or method != "item/completed":
            return []
        events = [
            *item_event(params, phase="started", turn_id=turn_id),
            *item_event(params, phase="completed", turn_id=turn_id),
        ]
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
        if method != "turn/completed" or status not in {"failed", "interrupted"}:
            return None
        thread = as_text(params.get("threadId"))
        if any(c.thread == thread and c.reported_failure for c in self.children):
            return None
        message = as_text(as_object(turn.get("error")).get("message"))
        detail = f": {message}" if message else ""
        return SubagentText(turn_id, call_id, None, f"Sub-agent turn {status}{detail}")


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
