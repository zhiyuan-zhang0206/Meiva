# meiva:note
# Inspect generated assistant text and build model-only visibility feedback.
# meiva:link base/agents/messages/docs/metadata-contracts.meiva.okf.md

from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage

from agent.messages import system_note_message
from base.agents.messages.kwargs import NoteTag, read_meiva_kwargs
from base.agents.messages.reply_intent import REPLY_VISIBILITY_CORRECTION, parse_reply_visibility
from base.host.boundary.json_values import is_object_dict
from base.lm.content import message_content
from base.lm.reasoning import to_canonical_reasoning


def has_invalid_reply_visibility(message: AIMessage) -> bool:
    if not read_meiva_kwargs(message).get("meiva_explicit_visibility"):
        return False
    content = to_canonical_reasoning(message_content(message))
    if isinstance(content, str):
        return bool(content.strip()) and parse_reply_visibility(content) is None
    return any(
        is_object_dict(block)
        and block.get("type") == "text"
        and isinstance(text := block.get("text"), str)
        and bool(text.strip())
        and parse_reply_visibility(text) is None
        for block in content
    )


def reply_visibility_note(now: datetime) -> HumanMessage:
    return system_note_message(
        content=REPLY_VISIBILITY_CORRECTION,
        tag=NoteTag.REPLY_VISIBILITY,
        picked_up_at=now,
        created_at=now,
    )
