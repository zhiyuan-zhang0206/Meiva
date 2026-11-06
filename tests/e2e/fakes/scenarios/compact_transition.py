from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes.scripted_model import ScriptedFakeChatModel, user_facing

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

FIRST_REPLY = "TRANSITION_FIRST_REPLY"
FIRST_TAIL = "TRANSITION_FIRST_TAIL"
FIRST_NARRATION = "TRANSITION_FIRST_NARRATION"
BETWEEN_REPLY = "TRANSITION_BETWEEN_REPLY"
BETWEEN_TAIL = "TRANSITION_BETWEEN_TAIL"
SECOND_NARRATION = "TRANSITION_SECOND_NARRATION"

# meiva:note
# The two summaries answer compaction calls, not turns; everything else is assistant text the
# user reads, so it opens with the display line.
_SCRIPT = tuple(
    AIMessage(content=text, usage_metadata=_USAGE)
    for text in (
        user_facing(FIRST_REPLY),
        user_facing(FIRST_TAIL + " filler" * 800),
        "TRANSITION_FIRST_SUMMARY",
        user_facing(FIRST_NARRATION),
        user_facing(BETWEEN_REPLY),
        user_facing(BETWEEN_TAIL + " filler" * 800),
        "TRANSITION_SECOND_SUMMARY",
        user_facing(SECOND_NARRATION),
    )
)


def build(model: str, *, agent_id: int | None) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=_SCRIPT)
