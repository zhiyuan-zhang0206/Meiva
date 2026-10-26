# meiva:note
# The one tool-call message helper that exec cancel tests share.


from langchain_core.messages import AIMessage


def ai_with_code(code: str) -> AIMessage:
    """Single tool wire format: AIMessage with execute_code tool_call."""
    return AIMessage(
        content="",
        tool_calls=[{"name": "execute_code", "args": {"code": code}, "id": "call_test"}],
    )
