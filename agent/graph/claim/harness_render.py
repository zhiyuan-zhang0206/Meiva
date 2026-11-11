from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from agent import state as _state
from base.agents.messages.kwargs import read_meiva_kwargs
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.content import message_content


# meiva:note
# Filter out agent replies from a sequence of messages.
# meiva:link agent/graph/docs/harness-turn.md
def pending_messages(
    messages_or_state: _state.AgentState | Sequence[BaseMessage],
) -> list[BaseMessage]:
    messages = (
        messages_or_state.messages
        if isinstance(messages_or_state, _state.AgentState)
        else messages_or_state
    )
    return [message for message in messages if not isinstance(message, (AIMessage, ToolMessage))]


def render_messages(messages: Sequence[BaseMessage]) -> str:
    return "\n\n".join(render_message(message) for message in messages).strip()


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _format_image_uploads_note(raw: object) -> str | None:
    if not is_object_list(raw) or not raw:
        return None
    lines = ["Attached images:"]
    for item in raw:
        if not is_object_dict(item):
            continue
        name = item.get("name")
        content_type = item.get("content_type")
        size = item.get("size")
        target = item.get("path") or item.get("url")
        if (
            isinstance(name, str)
            and isinstance(content_type, str)
            and isinstance(size, int)
            and not isinstance(size, bool)
            and isinstance(target, str)
        ):
            lines.append(f"- {name} ({content_type}, {_format_bytes(size)}): {target}")
    return "\n".join(lines) if len(lines) > 1 else None


def _strip_image_placeholder(text: str, *, body_start: int | None) -> str:
    if isinstance(body_start, int) and 0 <= body_start <= len(text):
        head = text[:body_start]
        body = text[body_start:]
        if body in {"[image]", "[files]"}:
            return head.rstrip()
    if text in {"[image]", "[files]"}:
        return ""
    return text


def render_message(message: BaseMessage) -> str:
    content = message_content(message)
    kwargs = read_meiva_kwargs(message)
    image_note = _format_image_uploads_note(kwargs.get("meiva_image_uploads"))
    body_start = kwargs.get("meiva_inbound_body_start")
    if isinstance(content, str):
        if image_note is None:
            return content
        cleaned = _strip_image_placeholder(
            content, body_start=body_start if isinstance(body_start, int) else None
        )
        return f"{cleaned}\n\n{image_note}" if cleaned.strip() else image_note
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(str(block["text"]))
        elif block.get("type") == "image_url" and image_note is not None:
            continue
        else:
            parts.append(f"[{block.get('type', 'attachment')}]")
    text = "\n".join(parts)
    if image_note is None:
        return text
    cleaned = _strip_image_placeholder(
        text, body_start=body_start if isinstance(body_start, int) else None
    )
    return f"{cleaned}\n\n{image_note}" if cleaned.strip() else image_note
