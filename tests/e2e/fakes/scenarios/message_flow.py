from __future__ import annotations

from langchain_core.messages import AIMessage

from scripts.verify.scripted_reply import REPLY_TEXT
from tests.e2e.fakes.scenario_recording import RecordingModel, note, say
from tests.e2e.fakes.scripted_model import ScriptedFakeChatModel, user_facing, working_note

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

UNMARKED_REPLY = "UNMARKED-TEXT-BEFORE-THE-CORRECTION"
HIDDEN_REPLY = "HIDDEN-WORKING-NOTE"
MARKED_REPLY = "MARKED-ANSWER-AFTER-THE-CORRECTION"

READING_REPLIES = tuple(
    "\n\n".join(
        f"Reading reply {number} paragraph {paragraph}. "
        + "This deterministic paragraph keeps a measurable reading position inside the message. "
        * 3
        for paragraph in range(1, 13)
    )
    for number in range(1, 4)
)

MESSAGE_FLOW_SCRIPT: tuple[AIMessage, ...] = (
    AIMessage(
        content=[
            {
                "type": "thinking",
                "thinking": "\u7528\u6237\u95ee 1+2 \u7b49\u4e8e\u51e0\uff0c\u5199\u4ee3\u7801\u7b97\u6700\u7a33\u3002",
                "index": 0,
            },
            {
                "type": "text",
                "text": working_note("\u6211\u6765\u7b97\u4e00\u4e0b\u3002"),
                "index": 1,
            },
        ],
        tool_calls=[
            {
                "id": "call_sum",
                "name": "execute_code",
                "args": {"code": "print(1 + 2)"},
            }
        ],
        usage_metadata=_USAGE,
    ),
    AIMessage(content=user_facing(REPLY_TEXT), usage_metadata=_USAGE),
)


def build(model: str, *, agent_id: int | None) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=MESSAGE_FLOW_SCRIPT)


def build_reading_reload(model: str, *, agent_id: int | None) -> ScriptedFakeChatModel:
    # meiva:note
    # Each native reply produces 108 primary rows so one anchored 50-item source
    # window keeps the whole row and crosses the 100-row window threshold. The
    # final block is tall enough to read inside; its stable message identity and
    # block index reach the real Saver/HTTP path.
    # meiva:link ui/web/src/docs/frontend-state/timeline-cache.md
    return ScriptedFakeChatModel(
        script=tuple(
            AIMessage(
                content=list[str | dict[str, str | int]](
                    [
                        {
                            "type": "text",
                            "text": user_facing(f"Reading context {number}, block {block}."),
                            "index": block,
                        }
                        for block in range(107)
                    ]
                    + [{"type": "text", "text": user_facing(reply), "index": 107}]
                ),
                usage_metadata=_USAGE,
            )
            for number, reply in enumerate(READING_REPLIES, start=1)
        )
    )


# meiva:note
# Both halves of the visibility rule in one script, kept beside the ordinary flow it varies:
# an answer with no visibility line and the corrected answer the correction note asks for (the
# second entry is only reached through that note, so reaching it proves the loop fired), then a
# reply the model marks hidden.
def build_visibility_correction(model: str, *, agent_id: int | None) -> RecordingModel:
    return RecordingModel(
        agent_id=agent_id,
        script=(
            AIMessage(content=UNMARKED_REPLY, usage_metadata=_USAGE),
            say(MARKED_REPLY),
            note(HIDDEN_REPLY),
        ),
    )
