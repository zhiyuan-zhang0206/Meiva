from __future__ import annotations

import pytest

from base.agents.messages.reply_intent import (
    NON_HUMAN_WAKE_REMINDER,
    USER_REPLY_HEADER,
    USER_REPLY_PREFIX,
    normalize_last_message_text,
    resolve_agent_reply,
    strip_user_reply_prefix,
)


def test_header_constants_match_contract() -> None:
    assert USER_REPLY_HEADER == "Meiva agent to user:"
    assert USER_REPLY_PREFIX == "Meiva agent to user: "
    assert USER_REPLY_PREFIX.startswith(USER_REPLY_HEADER)
    assert f"`{USER_REPLY_PREFIX}`" in NON_HUMAN_WAKE_REMINDER
    assert "\n" not in NON_HUMAN_WAKE_REMINDER


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Meiva agent to user: Hello there", "Hello there"),
        ("Meiva agent to user:Hello", "Hello"),
        ("  Meiva agent to user:   Hello\nworld", "Hello\nworld"),
        ("Meiva agent to user:\t  Hello", "Hello"),
        ("Meiva agent to user:\n  indented\n  next", "  indented\n  next"),
        ("Meiva agent to user: \r\n  indented", "  indented"),
        ("Meiva agent to user:\n\nParagraph", "\nParagraph"),
    ],
)
def test_strip_user_reply_prefix_removes_header_and_at_most_one_linebreak(
    raw: str, expected: str
) -> None:
    matched, body = strip_user_reply_prefix(raw)
    assert matched is True
    assert body == expected


@pytest.mark.parametrize(
    "raw",
    [
        "Plain reply",
        "meiva agent to user: lowercase does not match",
        "Note: Meiva agent to user: mid-sentence does not match",
        "Meiva agent to user without colon",
    ],
)
def test_strip_user_reply_prefix_ignores_non_matching_text(raw: str) -> None:
    matched, body = strip_user_reply_prefix(raw)
    assert matched is False
    assert body == raw


def test_resolve_agent_reply_polarity_matrix() -> None:
    assert resolve_agent_reply("Direct answer.", non_human_wake=False) == ("Direct answer.", True)
    assert resolve_agent_reply("Meiva agent to user: Direct answer.", non_human_wake=False) == (
        "Direct answer.",
        True,
    )
    assert resolve_agent_reply("Worker #42 finished.", non_human_wake=True) == (
        "Worker #42 finished.",
        False,
    )
    assert resolve_agent_reply("Meiva agent to user: PR #8015 is ready.", non_human_wake=True) == (
        "PR #8015 is ready.",
        True,
    )


@pytest.mark.parametrize("non_human_wake", [False, True])
@pytest.mark.parametrize(
    "empty_prefixed",
    [
        "Meiva agent to user:",
        "Meiva agent to user:   ",
        "Meiva agent to user:\n   \t",
    ],
)
def test_resolve_agent_reply_demotes_empty_after_strip_to_non_user_facing(
    empty_prefixed: str, non_human_wake: bool
) -> None:
    payload, user_facing = resolve_agent_reply(empty_prefixed, non_human_wake=non_human_wake)
    assert payload == empty_prefixed
    assert user_facing is False


def test_normalize_last_message_text_strips_prefix_and_drops_header_only() -> None:
    assert normalize_last_message_text("Plain text") == "Plain text"
    assert normalize_last_message_text("Meiva agent to user: Shipped.") == "Shipped."
    assert normalize_last_message_text("Meiva agent to user:   \n ") == ""
