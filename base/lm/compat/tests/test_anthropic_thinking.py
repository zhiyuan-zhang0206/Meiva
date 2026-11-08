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


def test_is_foreign_thinking_signature() -> None:
    from base.lm.compat.anthropic_thinking import is_foreign_thinking_signature

    assert is_foreign_thinking_signature("9d154c27-92a4-4903-b842-e98dc9e20cb1")
    assert is_foreign_thinking_signature("opaque")
    assert is_foreign_thinking_signature(None)
    assert is_foreign_thinking_signature(123)
    real_sig = (
        "CAQS6QQKEAgTGAI4AUIIdGhpbmtpbmcSDEWCdz8HUZep1rKU9BoMqtEwVUMVL2tBarjsIjAWV9dPQQiZOhou"
    )
    assert not is_foreign_thinking_signature(real_sig)


def test_sanitize_anthropic_messages_drops_foreign_thinking() -> None:
    from langchain_core.messages import AIMessage, HumanMessage

    from base.lm.compat.anthropic_thinking import sanitize_anthropic_messages

    uuid_sig = "2668f888-1c8d-4655-94ac-50c9a3bf0011"
    real_sig = (
        "CAQS6QQKEAgTGAI4AUIIdGhpbmtpbmcSDEWCdz8HUZep1rKU9BoMqtEwVUMVL2tBarjsIjAWV9dPQQiZOhou"
    )

    messages = [
        HumanMessage(content="Hello"),
        AIMessage(
            content=[
                {"type": "thinking", "thinking": "deepseek thinking", "signature": uuid_sig},
                {"type": "text", "text": "hello from deepseek"},
            ]
        ),
        AIMessage(
            content=[
                {"type": "thinking", "thinking": "claude thinking", "signature": real_sig},
                {"type": "text", "text": "hello from claude"},
            ]
        ),
        AIMessage(
            content=[
                {"type": "thinking", "thinking": "only thinking", "signature": uuid_sig},
            ]
        ),
    ]

    sanitized = sanitize_anthropic_messages(messages)
    assert len(sanitized) == 4
    assert sanitized[1].content == [{"type": "text", "text": "hello from deepseek"}]
    assert sanitized[2].content == [
        {"type": "thinking", "thinking": "claude thinking", "signature": real_sig},
        {"type": "text", "text": "hello from claude"},
    ]
    assert sanitized[3].content == [{"type": "text", "text": ""}]


def test_sanitize_anthropic_messages_downscales_multi_images() -> None:
    from langchain_core.messages import HumanMessage

    from base.lm.compat.anthropic_thinking import sanitize_anthropic_messages

    data_uri = "data:image/png;base64,large_fake_data"

    def mock_downscaler(url: str, max_px: int) -> str:
        assert max_px == 3000
        return "data:image/png;base64,downscaled_data"

    single_msgs = [HumanMessage(content=[{"type": "image_url", "image_url": {"url": data_uri}}])]
    result_single = sanitize_anthropic_messages(single_msgs, image_downscaler=mock_downscaler)
    c0 = result_single[0].content
    assert isinstance(c0, list) and isinstance(c0[0], dict)
    assert c0[0]["image_url"]["url"] == data_uri

    multi_msgs = [
        HumanMessage(content=[{"type": "image_url", "image_url": {"url": data_uri}}]),
        HumanMessage(content=[{"type": "image_url", "image_url": {"url": data_uri}}]),
    ]
    result_multi = sanitize_anthropic_messages(multi_msgs, image_downscaler=mock_downscaler)
    m0 = result_multi[0].content
    m1 = result_multi[1].content
    assert isinstance(m0, list) and isinstance(m0[0], dict)
    assert isinstance(m1, list) and isinstance(m1[0], dict)
    assert m0[0]["image_url"]["url"] == "data:image/png;base64,downscaled_data"
    assert m1[0]["image_url"]["url"] == "data:image/png;base64,downscaled_data"
