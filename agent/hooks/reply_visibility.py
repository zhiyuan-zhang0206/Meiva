# meiva:note
# Deliver visibility corrections after an assistant's tool results have committed.
# meiva:link base/agents/messages/docs/metadata-contracts.meiva.okf.md

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent import state as _state
from agent.hooks._registry import Hook
from agent.messages.reply_visibility import has_invalid_reply_visibility, reply_visibility_note
from base.agents.context import MeivaContext
from base.agents.messages.kwargs import NoteTag, read_meiva_kwargs


class _DeliverReplyVisibilityCorrection(Hook):
    async def __call__(
        self,
        state: _state.AgentState,
        runtime: Runtime[MeivaContext],
        _config: RunnableConfig,
        /,
    ) -> dict[str, object] | None:
        for message in reversed(state.messages):
            if isinstance(message, AIMessage):
                if message.tool_calls and has_invalid_reply_visibility(message):
                    return {
                        "messages": [reply_visibility_note(runtime.context.require_clock().now())]
                    }
                return None
            if read_meiva_kwargs(message).get("meiva_note_tag") == NoteTag.REPLY_VISIBILITY:
                return None
        return None


deliver_reply_visibility_correction = _DeliverReplyVisibilityCorrection()
