from __future__ import annotations

from langchain_core.messages import AIMessage

from tests.e2e.fakes.scripted_model import ScriptedFakeChatModel, user_facing

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def _turn(i: int) -> AIMessage:
    return AIMessage(
        content=[
            {"type": "thinking", "thinking": f"\u7b2c {i} \u6b65\u601d\u8003\u3002", "index": 0},
            {
                "type": "text",
                # meiva:note
                # This scenario exists to page through a long, readable conversation, so each
                # step's narration is a reply the user reads. Folded into Details instead, the
                # whole history renders as one collapsed row and there is no reading position
                # left to preserve.
                # It is a paging fixture, not a reference for how much narration belongs in the
                # main conversation: routine tool narration is a working note (`working_note`).
                # Do not copy this shape into a scenario about what the user should see.
                "text": user_facing(f"\u6267\u884c\u7b2c {i} \u6b65\u3002" + " step detail" * 40),
                "index": 1,
            },
        ],
        tool_calls=[
            {
                "id": f"call_{i}",
                "name": "execute_code",
                "args": {"code": f"print({i})"},
            }
        ],
        usage_metadata=_USAGE,
    )


LOAD_OLDER_SCRIPT: tuple[AIMessage, ...] = (
    *(_turn(i) for i in range(1, 36)),
    AIMessage(
        content=user_facing("\u5168\u90e8\u6267\u884c\u5b8c\u6bd5\u3002"), usage_metadata=_USAGE
    ),
)


def build(model: str, *, agent_id: int | None) -> ScriptedFakeChatModel:
    return ScriptedFakeChatModel(script=LOAD_OLDER_SCRIPT)
