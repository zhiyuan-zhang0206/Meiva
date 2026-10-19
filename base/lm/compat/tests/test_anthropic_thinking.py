"""base/lm/compat/anthropic_thinking.py — ThinkingTokensChatAnthropic preserves thinking_tokens.

Verifies that the _make_message_chunk_from_anthropic_event override captures
output_tokens_details.thinking_tokens from the message_delta stream event
and adds it to usage_metadata.output_token_details.reasoning.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from langchain_core.messages import AIMessageChunk, UsageMetadata

from base.lm.compat.anthropic_thinking import retain_usage_details


def _message_delta_event(*, thinking_tokens: int) -> MagicMock:
    """Build a mock message_delta event carrying thinking_tokens."""
    event = MagicMock()
    event.type = "message_delta"
    output_details = MagicMock()
    output_details.thinking_tokens = thinking_tokens
    event.usage.output_tokens_details = output_details
    return event


def test_message_delta_with_thinking_tokens_sets_reasoning():
    """A message_delta event with thinking_tokens > 0 populates
    output_token_details.reasoning on the returned chunk."""
    event = _message_delta_event(thinking_tokens=1234)
    msg = AIMessageChunk(
        content=[],
        usage_metadata=UsageMetadata(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        ),
    )
    retained = retain_usage_details(msg, event)
    assert retained is msg and msg.usage_metadata is not None

    details = msg.usage_metadata.get("output_token_details") or {}
    assert details.get("reasoning") == 1234


def test_message_delta_zero_thinking_tokens_noop():
    """thinking_tokens == 0 → no output_token_details added."""
    event = _message_delta_event(thinking_tokens=0)
    msg = AIMessageChunk(
        content=[],
        usage_metadata=UsageMetadata(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        ),
    )
    retain_usage_details(msg, event)
    assert msg.usage_metadata is not None
    # No output_token_details set because thinking_tokens == 0
    assert msg.usage_metadata.get("output_token_details") is None


def test_non_message_delta_event_noop():
    """Non-message_delta events (e.g. content_block_delta) are not modified."""
    event = MagicMock()
    event.type = "content_block_delta"
    msg = AIMessageChunk(
        content=[],
        usage_metadata=UsageMetadata(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        ),
    )
    retain_usage_details(msg, event)
    assert msg.usage_metadata is not None
    assert msg.usage_metadata.get("output_token_details") is None


def test_reasoning_already_present_no_overwrite():
    """If output_token_details.reasoning is already set, don't overwrite."""
    event = _message_delta_event(thinking_tokens=999)
    msg = AIMessageChunk(
        content=[],
        usage_metadata=UsageMetadata(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            output_token_details={"reasoning": 42},
        ),
    )
    retain_usage_details(msg, event)
    assert msg.usage_metadata is not None
    # Should keep the original value, not overwrite
    details = msg.usage_metadata.get("output_token_details") or {}
    assert details.get("reasoning") == 42


def test_a_none_chunk_stays_none() -> None:
    assert retain_usage_details(None, _message_delta_event(thinking_tokens=500)) is None
