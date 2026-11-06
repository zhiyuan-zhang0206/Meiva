from __future__ import annotations

import pytest

from base.agents.messages.reply_intent import (
    DISPLAY_TO_USER,
    HIDE_FROM_USER,
    NON_HUMAN_WAKE_REMINDER,
    USER_REPLY_HEADER,
    USER_REPLY_PREFIX,
    normalize_last_message_text,
    parse_reply_visibility,
    resolve_agent_reply,
    strip_user_reply_prefix,
)


def test_header_constants_match_contract() -> None:
    assert USER_REPLY_HEADER == "Meiva agent to user:"
    assert USER_REPLY_PREFIX == "Meiva agent to user: "
    assert USER_REPLY_PREFIX.startswith(USER_REPLY_HEADER)
    assert DISPLAY_TO_USER in NON_HUMAN_WAKE_REMINDER
    assert HIDE_FROM_USER in NON_HUMAN_WAKE_REMINDER
    assert "no assistant text" not in NON_HUMAN_WAKE_REMINDER
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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("MEIVA_DISPLAY_TO_USER\nShipped.", ("Shipped.", True)),
        ("MEIVA_HIDE_FROM_USER\r\nWork log.", ("Work log.", False)),
        ("MEIVA_DISPLAY_TO_USER", ("", False)),
        ("MEIVA_H", ("", False)),
        ("MEIVA_HIDE_FROM_USER\r", ("", False)),
        ("MEIVA_DISPLAY_TO_USER\n   ", ("", False)),
        ("**MEIVA_DISPLAY_TO_USER**\nShipped.", ("**MEIVA_DISPLAY_TO_USER**\nShipped.", False)),
        ("Missing header.", ("Missing header.", False)),
    ],
)
@pytest.mark.parametrize("non_human_wake", [False, True])
def test_explicit_visibility_routes_by_exact_first_line(
    text: str, expected: tuple[str, bool], non_human_wake: bool
) -> None:
    assert (
        resolve_agent_reply(text, non_human_wake=non_human_wake, explicit_visibility=True)
        == expected
    )


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize(
    ("mode", "text", "visible", "body"),
    [
        ("naive", "ordinary answer", True, "ordinary answer"),
        ("naive", "MEIVA_H", True, "MEIVA_H"),
        ("naive", "MEIVA_HIDE_FROM_USER\nverbatim", True, "MEIVA_HIDE_FROM_USER\nverbatim"),
        ("prefix", "ordinary answer", False, "ordinary answer"),
        ("prefix", "MEIVA_DISPLAY_TO_USER\nanswer", True, "answer"),
        ("sdk", "ordinary final", False, "ordinary final"),
        ("sdk", "MEIVA_H", False, "MEIVA_H"),
        ("sdk", "MEIVA_DISPLAY_TO_USER\nanswer", False, "MEIVA_DISPLAY_TO_USER\nanswer"),
    ],
)
def test_delivery_mode_overrides_wake_and_legacy_visibility(
    mode: str, text: str, visible: bool, body: str, background: bool
) -> None:
    assert resolve_agent_reply(
        text, non_human_wake=background, explicit_visibility=True, delivery_mode=mode
    ) == (body, visible)


def test_unknown_delivery_mode_fails_at_boundary() -> None:
    with pytest.raises(ValueError):
        resolve_agent_reply("answer", non_human_wake=False, delivery_mode="typo")


@pytest.mark.parametrize("text", ["MEIVA_H", "MEIVA_DISPLAY_TO_USER", "MEIVA_HIDE_FROM_USER\r\n  "])
def test_incomplete_visibility_marker_is_still_invalid(text: str) -> None:
    assert parse_reply_visibility(text) is None
    assert resolve_agent_reply(text, non_human_wake=False, delivery_mode="prefix") == ("", False)
    assert resolve_agent_reply(text, non_human_wake=False) == (text, True)
