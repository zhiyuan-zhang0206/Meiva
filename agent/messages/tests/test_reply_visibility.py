from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolCall

from agent.messages.reply_visibility import has_invalid_reply_visibility
from base.agents.messages.reply_intent import REPLY_VISIBILITY_CORRECTION


def _message(content: str, *, tool_calls: bool = False, **kwargs: object) -> AIMessage:
    calls = [ToolCall(name="execute_code", args={}, id="call-1")] if tool_calls else []
    return AIMessage(
        content=content,
        tool_calls=calls,
        additional_kwargs={"meiva_explicit_visibility": True, **kwargs},
    )


def test_an_unmarked_final_reply_to_a_human_is_invalid() -> None:
    assert has_invalid_reply_visibility(_message("The answer.")) is True
    assert has_invalid_reply_visibility(_message("MEIVA_DISPLAY_TO_USER\nThe answer.")) is False


def test_an_unmarked_reply_to_a_non_human_wake_is_valid() -> None:
    assert has_invalid_reply_visibility(_message("Noted.", meiva_non_human_wake=True)) is False


def test_unmarked_narration_beside_tool_calls_is_valid() -> None:
    assert has_invalid_reply_visibility(_message("Reading the file.", tool_calls=True)) is False


def test_history_without_explicit_visibility_is_never_invalid() -> None:
    assert has_invalid_reply_visibility(AIMessage(content="Old reply.")) is False


@pytest.mark.parametrize(
    "phrase", ["final reply to the user's message", "folded into Details", "visibility prefix"]
)
def test_the_correction_names_the_reply_it_asks_to_resend(phrase: str) -> None:
    assert phrase in REPLY_VISIBILITY_CORRECTION
    assert "If it was meant for the user, resend it with the display prefix" in (
        REPLY_VISIBILITY_CORRECTION
    )
