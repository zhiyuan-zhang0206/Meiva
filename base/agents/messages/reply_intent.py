from __future__ import annotations

# meiva:note
# Explicit user-reply header for background/non-human wakes. `USER_REPLY_PREFIX` is the canonical
# opening taught in the system prompt and exported to the frontend via `dump_frontend_constants.py`;
# `USER_REPLY_HEADER` is the exact colon-terminated token matched by both backend timeline
# projection and frontend SSE stream folding.
# meiva:link base/agents/messages/docs/metadata-contracts.meiva.okf.md
USER_REPLY_HEADER = "Meiva agent to user:"
USER_REPLY_PREFIX = "Meiva agent to user: "

# meiva:note
# Appended once in `claim` when an agent enters a `non_human_wake=True` run so long
# multi-step sessions retain a recency signal beside the inbound wake envelope without
# repeating on mid-run tool loops or mid-run secondary arrivals.
# meiva:link base/agents/messages/docs/metadata-contracts.meiva.okf.md
NON_HUMAN_WAKE_REMINDER = (
    "Non-human wake: unprefixed assistant text folds into the collapsed work log. "
    "End the turn with no assistant text when nothing needs the human now, or begin "
    f"user-intended text with `{USER_REPLY_PREFIX}`."
)


# meiva:note
# Leading whitespace before the header is tolerated. Horizontal whitespace after the colon
# and at most one line break (`\r\n` or `\n`) are stripped so both `Meiva agent to user: Hello`
# and `Meiva agent to user:\nHello` render cleanly while preserving indentation on later lines.
# meiva:link base/agents/messages/docs/metadata-contracts.meiva.okf.md
def strip_user_reply_prefix(text: str) -> tuple[bool, str]:
    trimmed = text.lstrip()
    if not trimmed.startswith(USER_REPLY_HEADER):
        return False, text
    rest = trimmed[len(USER_REPLY_HEADER) :]
    idx = 0
    while idx < len(rest) and rest[idx] in (" ", "\t"):
        idx += 1
    if idx < len(rest) and rest[idx] == "\r":
        idx += 1
        if idx < len(rest) and rest[idx] == "\n":
            idx += 1
    elif idx < len(rest) and rest[idx] == "\n":
        idx += 1
    return True, rest[idx:]


# meiva:note
# Header-only output with nothing non-blank after stripping stays `(text, False)` inside
# `Details` rather than promoting an empty primary bubble, keeping `item_id` alignment intact.
# meiva:link base/agents/messages/docs/metadata-contracts.meiva.okf.md
def resolve_agent_reply(text: str, *, non_human_wake: bool) -> tuple[str, bool]:
    matched, body = strip_user_reply_prefix(text)
    if matched:
        if body.strip():
            return body, True
        return text, False
    return text, not non_human_wake


# meiva:link base/agents/messages/docs/metadata-contracts.meiva.okf.md
def normalize_last_message_text(text: str) -> str:
    matched, body = strip_user_reply_prefix(text)
    if not matched:
        return text
    return body if body.strip() else ""
