"""Agent-runtime face of the meiva_sdk_reminder plugin — state fields and hooks.

Loaded only in the agent process: the plugin loader (`base.packages.plugins.loading`) imports this module after
`plugin.py` on the full path (host boot / graph build). The plugin has no
SDK-visible surface — everything it does is agent-runtime behavior — so the
surface module carries only the description (task #3633).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph.hooks.compact import auto_compact_will_fire
from agent.graph.tool_calls import first_tool_call_code
from agent.hooks import Hook
from agent.messages import NoteTag, system_note_message, tail_has_agent_inbound
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.lm.content import message_content
from base.log import logger
from base.packages.plugins.extensions import PluginContributions

from ._state import (
    AGENT_REPLY_CATEGORY as AGENT_REPLY_CATEGORY,
)
from ._state import (
    AGENT_REPLY_HINT,
    MeivaSdkReminderState,
)
from ._state import (
    CATEGORIES as CATEGORIES,
)
from ._state import (
    detect_categories as detect_categories,
)
from ._state import (
    hint_for as hint_for,
)
from ._state import (
    mentions_watcher as mentions_watcher,
)
from .hint_selection import assumed_persistence_name, rearmed_reminded, select_hints

# Channel keys for this plugin's state fields (prefixed by the framework from the
# declared state class). A hook node reads/writes these directly on `state`
# because the in-turn handle (state_handle.read/update) is not wired up here.
_REMINDED_FIELD = "meiva_sdk_reminder__reminded"
_BOOKMARK_FIELD = "meiva_sdk_reminder__last_seen_compact"


def _earlier_codes(previous_messages: list[AnyMessage]) -> list[str]:
    return [
        first_tool_call_code([call])
        for message in previous_messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["name"] == "execute_code"
    ]


def _rearmed(state: AgentState) -> tuple[set[str], int]:
    reminded, bookmark = rearmed_reminded(
        getattr(state, _REMINDED_FIELD), getattr(state, _BOOKMARK_FIELD), state.compact.version
    )
    return set(reminded), bookmark


class _SdkReminderAfterExecHook(Hook):
    """Inject an SDK-primitive or interpreter-persistence note as its own
    system-styled message after the matching execution output.

    The note is a separate `system_note_message`, not text appended to the
    execution output: an appended line reads as the cell's own stdout, which
    the agent takes for normal program output, whereas a system note reads as a
    framework aside. The exec-output message is left untouched.

    The wait category is special-cased: a cell that sleeps while already naming
    `watcher` is the agent working with the watcher primitive itself, so the
    wait hint is suppressed (marked seen without emitting) rather than nagging.

    No-op (returns None) when the message tail does not match the
    assistant-call + execution-output shape, when neither a native idiom nor a
    qualifying NameError matches, or when every match is suppressed or already
    recorded under its once-per-compaction cadence.
    """

    async def __call__(
        self, state: AgentState, runtime: Runtime[MeivaContext], config: RunnableConfig, /
    ) -> dict[str, Any] | None:
        """Match each result by ID, retaining the per-compaction hint cadence."""
        ai_index = next(
            (
                i
                for i in range(len(state.messages) - 1, -1, -1)
                if isinstance(state.messages[i], AIMessage)
            ),
            None,
        )
        if ai_index is None:
            return None
        ai = state.messages[ai_index]
        assert isinstance(ai, AIMessage)  # noqa: S101
        outputs = {
            msg.tool_call_id: msg
            for msg in state.messages[ai_index + 1 :]
            if isinstance(msg, ToolMessage)
        }
        prior = state.messages[:ai_index]
        update: dict[str, Any] = {}
        notes: list[AnyMessage] = []
        for call in ai.tool_calls:
            output = outputs.get(call["id"] or "")
            if output is None:
                continue
            single = ai.model_copy(update={"tool_calls": [call]})
            view = state.model_copy(update={**update, "messages": [*prior, single, output]})
            delta = await self._for_call(view, runtime, config)
            if delta:
                notes.extend(delta.pop("messages", []))
                update.update(delta)
            prior = [*prior, single, output]
        if notes:
            update["messages"] = notes
        return update or None

    async def _for_call(
        self,
        state: AgentState,
        runtime: Runtime[MeivaContext],
        _config: RunnableConfig,
        /,
    ) -> dict[str, Any] | None:
        if len(state.messages) < 2:
            return None
        ai_msg = state.messages[-2]
        out_msg = state.messages[-1]
        if not isinstance(ai_msg, AIMessage) or not isinstance(out_msg, ToolMessage):
            return None

        # tool_calls is a pydantic field on AIMessage (always present, empty list
        # when the model called nothing); first_tool_call_code returns "" for an
        # empty list or a non-string/absent code arg.
        code = first_tool_call_code(ai_msg.tool_calls)
        if not code:
            return None

        reminders = runtime.context.require_agent().sdk_reminders
        matched = detect_categories(code)
        output = message_content(out_msg)
        nameerror_name = (
            assumed_persistence_name(output, _earlier_codes(state.messages[:-2]))
            if reminders.sdk_nameerror_hint_enabled and isinstance(output, str)
            else None
        )
        if not matched and nameerror_name is None:
            return None

        reminded, new_bookmark = _rearmed(state)

        selection = select_hints(
            code,
            matched,
            reminded,
            nameerror_name,
            every_time=reminders.sdk_code_reminder_cadence == "every_time",
        )
        if not selection.newly_seen:
            # Every once-scoped match is already seen this window (or silently
            # suppressed and already marked). The bookmark only advances on a path
            # that records a fresh key, and a re-arm clears `reminded` (making every
            # once-scoped match fresh again) — so this no-op never strands a pending
            # bookmark advance.
            return None

        update: dict[str, Any] = {
            _REMINDED_FIELD: reminded | selection.newly_seen,
            _BOOKMARK_FIELD: new_bookmark,
        }
        if selection.hinted or selection.nameerror_hint is not None:
            hint_lines = [hint_for(cat) for cat in selection.hinted]
            if selection.nameerror_hint is not None:
                hint_lines.append(selection.nameerror_hint)
            hints = "\n".join(hint_lines)
            # Emit the hint as its own system-styled note appended after the
            # exec-output message (a fresh message with no id, so the reducer adds
            # rather than replaces). Splicing it onto out_msg.content would read as
            # the cell's own stdout; a system_note reads as a framework aside, the
            # same surface the agent_reply / compact-reminder notes use. The
            # exec-output message keeps carrying only the agent's real output.
            now = runtime.context.require_clock().now()
            update["messages"] = [
                system_note_message(
                    content=hints,
                    tag=NoteTag.SDK_HINT,
                    picked_up_at=now,
                    created_at=now,
                )
            ]
        return update


# meiva:note
# The system-styled note pointing at `meiva.agents.send_message`, stamped `now`.
def _agent_reply_note(now: datetime) -> HumanMessage:
    return system_note_message(
        content=AGENT_REPLY_HINT, tag=NoteTag.AGENT_REPLY, picked_up_at=now, created_at=now
    )


class _SdkReminderAgentReplyHook(Hook):
    """Append the note pointing at the agent->agent delivery primitive when the
    incoming batch holds a message from another agent.

    Runs before the reply is produced (a plain text reply runs no code). The
    firing cadence is `agent_reply_reminder_cadence`:
    - `once_per_compaction` (default): fire at most once per context window; a
      compaction re-arms it (the shared `reminded` set / bookmark, same as the
      code categories).
    - `every_time`: fire on every agent inbound (for agents that keep
      forgetting to use the SDK). The category does not join `reminded`.

    No-op (returns None) when the new tail has no agent-sourced inbound, when
    auto-compact would fire this same turn (either cadence — the note would be
    clobbered by compaction's message replacement; skip this inbound, leaving
    the category unmarked so the next agent inbound still qualifies), or, in
    the once cadence, when agent_reply was already hinted this window.
    """

    async def __call__(
        self,
        state: AgentState,
        runtime: Runtime[MeivaContext],
        _config: RunnableConfig,
        /,
    ) -> dict[str, Any] | None:
        if not tail_has_agent_inbound(state.messages):
            return None

        # Defer if auto-compact will replace messages this same turn (both
        # cadences). `messages` carries the add_messages reducer, so co-writing it
        # merges rather than fail-louding; but compaction's full-history REMOVE_ALL
        # replacement is order-sensitive and would drop a note appended here. So
        # when compaction is predicted this turn, skip this inbound entirely: the
        # tail scan next turn stops at the agent's own AIMessage (this inbound is
        # then behind that boundary, already past), so the reminder effectively waits
        # for the *next* agent inbound. Leave AGENT_REPLY_CATEGORY unmarked so a
        # future inbound still qualifies.
        agent = runtime.context.require_agent()
        if auto_compact_will_fire(state, agent, catalog=runtime.context.require_catalog()):
            logger.info(
                "[sdk-reminder] defer: auto-compact predicted, skipping agent-inbound hint this turn"
            )
            return None

        # The Literal config validates at Settings construction (an unknown value
        # fails fast there), so the match is exhaustive — a new cadence added to the
        # Literal turns this into a static non-exhaustive error rather than a silent
        # fall-through.
        match agent.sdk_reminders.agent_reply_reminder_cadence:
            case "every_time":
                return {"messages": [_agent_reply_note(runtime.context.require_clock().now())]}
            case "once_per_compaction":
                reminded, new_bookmark = _rearmed(state)
                if AGENT_REPLY_CATEGORY in reminded:
                    # Already hinted, and the re-arm only clears `reminded` (which would
                    # drop AGENT_REPLY_CATEGORY out of this set), so a bookmark advance
                    # and "still reminded" cannot co-occur — nothing to persist, no-op.
                    return None
                reminded.add(AGENT_REPLY_CATEGORY)
                return {
                    "messages": [_agent_reply_note(runtime.context.require_clock().now())],
                    _REMINDED_FIELD: reminded,
                    _BOOKMARK_FIELD: new_bookmark,
                }


sdk_reminder_agent_reply_before_llm = _SdkReminderAgentReplyHook()


def contribute() -> PluginContributions:
    """What this plugin declares for the agent runtime."""
    return PluginContributions(
        before_llm=(sdk_reminder_agent_reply_before_llm,),
        after_exec=(_SdkReminderAfterExecHook(),),
        state=(MeivaSdkReminderState,),
    )
