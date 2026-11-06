from __future__ import annotations

from base.api_contracts.user_delivery import UserDeliveryMode

# meiva:note
# Explicit user-reply header for background/non-human wakes. `USER_REPLY_PREFIX` is the canonical
# opening taught in the system prompt and exported to the frontend via `dump_frontend_constants.py`;
# `USER_REPLY_HEADER` is the exact colon-terminated token matched by both backend timeline
# projection and frontend SSE stream folding.
# meiva:link base/agents/messages/docs/metadata-contracts.meiva.okf.md
USER_REPLY_HEADER = "Meiva agent to user:"
USER_REPLY_PREFIX = "Meiva agent to user: "

DISPLAY_TO_USER = "MEIVA_DISPLAY_TO_USER"
HIDE_FROM_USER = "MEIVA_HIDE_FROM_USER"
REPLY_VISIBILITY_RULE = (
    f"Start each non-empty assistant text block with either {DISPLAY_TO_USER} or "
    f"{HIDE_FROM_USER} on its own first line, then write the body on the next line. "
    f"Use {DISPLAY_TO_USER} for answers, deliverables, decisions and meaningful milestones "
    f"the user needs to see in the main conversation. Use {HIDE_FROM_USER} for working notes. "
    "Those notes are folded into Details and the user can expand and read them."
)
NON_HUMAN_WAKE_REMINDER = f"Non-human wake. {REPLY_VISIBILITY_RULE}"
REPLY_VISIBILITY_CORRECTION = (
    "Your previous output contained text with a missing or invalid visibility prefix. "
    "That text was folded into Details. "
    f"{REPLY_VISIBILITY_RULE} If it was a user-facing reply, resend it with the display prefix."
)


def _resolve_prefix_reply(text: str) -> tuple[str, bool] | None:
    header, separator, body = text.partition("\n")
    header = header.removesuffix("\r")
    if header in (DISPLAY_TO_USER, HIDE_FROM_USER):
        return (body, header == DISPLAY_TO_USER) if body.strip() else ("", False)
    if not separator and (DISPLAY_TO_USER.startswith(text) or HIDE_FROM_USER.startswith(text)):
        return "", False
    return None


def parse_reply_visibility(text: str) -> tuple[str, bool] | None:
    resolved = _resolve_prefix_reply(text)
    return resolved if resolved is not None and resolved[0] else None


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
# A legacy wake header with a blank body keeps its raw text in Details.
# Prefix-mode marker fragments keep their item identity with an empty payload.
# meiva:link base/agents/messages/docs/metadata-contracts.meiva.okf.md
def resolve_agent_reply(
    text: str,
    *,
    non_human_wake: bool,
    explicit_visibility: bool = False,
    delivery_mode: str | None = None,
) -> tuple[str, bool]:
    if delivery_mode is not None:
        mode = UserDeliveryMode(delivery_mode)
        if mode is UserDeliveryMode.NAIVE:
            return text, bool(text.strip())
        if mode is UserDeliveryMode.SDK:
            return text, False
        return _resolve_prefix_reply(text) or (text, False)
    if explicit_visibility:
        return _resolve_prefix_reply(text) or (text, False)
    matched, body = strip_user_reply_prefix(text)
    if matched:
        if body.strip():
            return body, True
        return text, False
    return text, not non_human_wake


# meiva:link base/agents/messages/docs/metadata-contracts.meiva.okf.md
def normalize_last_message_text(text: str) -> str:
    explicit = parse_reply_visibility(text)
    if explicit is not None:
        return explicit[0]
    matched, body = strip_user_reply_prefix(text)
    if not matched:
        return text
    return body if body.strip() else ""


SDK_DELIVERY_RULE = (
    "Deliver user-facing information through meiva.ui.publish(content, "
    "suggestions=[...]). Suggestions are optional candidates "
    "for the independent composer service. Ask questions with meiva.ui.ask(title, "
    "content, question={...}, note_suggestion=...). Every question "
    "has a user-editable Note. Publishing or asking never ends execution: continue "
    "independent work. Ordinary assistant text, including final summaries, is folded "
    "into Details which the user can expand. Before a natural turn end, publish, ask, "
    "or explicitly record no update with meiva.ui.publish(None). "
    "Reading is not answering or authorizing. Questions persist until answered or closed."
)


def delivery_disabled_entries(mode: str) -> tuple[str, ...]:
    return (
        ("ui.notify", "ui.edit_notice", "ui.dismiss_notice")
        if UserDeliveryMode(mode) is UserDeliveryMode.SDK
        else ("ui.publish", "ui.publications")
    )


def non_human_delivery_reminder(mode: str) -> str:
    return {
        UserDeliveryMode.PREFIX: NON_HUMAN_WAKE_REMINDER,
        UserDeliveryMode.SDK: SDK_DELIVERY_RULE,
        UserDeliveryMode.NAIVE: "Non-human wake. Communicate only meaningful changes.",
    }[UserDeliveryMode(mode)]
