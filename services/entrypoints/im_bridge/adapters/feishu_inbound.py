"""Feishu public SDK event payloads normalized at the IM boundary."""

from collections.abc import Collection

from pydantic import BaseModel, ConfigDict

from services.entrypoints.im_bridge.adapters import feishu_poll_cursor as cursors
from services.entrypoints.im_bridge.adapters.feishu_poll_cursor import (
    FeishuListedMessage,
    FeishuOpenIdHolder,
    FeishuSender,
    FeishuView,
)
from services.entrypoints.im_bridge.types import InboundMessage


class _EventMessage(FeishuView):
    message_id: str | None = None
    chat_type: str | None = None
    message_type: str | None = None
    content: str | None = None


class _MessageEvent(FeishuView):
    message: _EventMessage | None = None
    sender: FeishuSender | None = None


# meiva:note
# A `P2ImMessageReceiveV1`, or a stand-in that carries the same attributes.
class _MessageReceived(FeishuView):
    event: _MessageEvent | None = None


class _CardValue(FeishuView):
    value: dict[str, object] | None = None


class _CardActionEvent(FeishuView):
    operator: FeishuOpenIdHolder | None = None
    action: _CardValue | None = None


# meiva:note
# A card action trigger event, or a stand-in that carries the same attributes.
class _CardActionReceived(FeishuView):
    event: _CardActionEvent | None = None


class _TextContent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    text: str


class _OptionalTextContent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    text: object = ""


def normalize_message(data: object, seen_messages: Collection[str]) -> InboundMessage | None:
    """Map a ``P2ImMessageReceiveV1`` (or duck-typed stand-in) to an
    InboundMessage; return None for events we do not bridge."""
    event = _MessageReceived.model_validate(data).event
    if event is None or event.message is None or event.sender is None:
        return None
    message, sender = event.message, event.sender
    if message.chat_type != "p2p":
        return None  # group chats are not bridged
    if message.message_type != "text":
        return None  # only plain text is bridged
    if sender.sender_type != "user":
        return None  # the bot's own messages must not echo back into core
    text = _TextContent.model_validate_json(message.content or "").text.strip()
    if not text:
        return None
    open_id = sender.sender_id.open_id if sender.sender_id is not None else ""
    if not open_id:
        return None
    message_id = message.message_id
    # The polling fallback may have already fed this message (or vice
    # versa): a shared seen-set makes the two paths idempotent.
    if message_id in seen_messages:
        return None
    return InboundMessage(
        channel="feishu",
        chat_id=open_id,  # contract: the feishu session IS the user's open_id
        text=text,
        message_id=message_id,
        idempotency_key=cursors.idempotency_key(message_id),
    )


def normalize_card_action(data: object) -> InboundMessage | None:
    action = _CardActionReceived.model_validate(data).event
    if action is None:
        return None
    open_id = action.operator.open_id if action.operator is not None else ""
    if not open_id:
        return None
    if action.action is None:
        return None
    key = str((action.action.value or {}).get("key", ""))
    if not key:
        return None
    return InboundMessage(channel="feishu", chat_id=open_id, text=key)


# meiva:note
# Extract the sender's open id from either the WS event shape or the ListMessage API shape.
def sender_open_id(sender: FeishuSender) -> str:
    open_id = sender.sender_id.open_id if sender.sender_id is not None else ""
    if not open_id and sender.id_type == "open_id":
        open_id = sender.id
    return open_id or ""


# meiva:note
# Map one listed message to an InboundMessage across both WS and ListMessage shapes.
def normalize_poll_item(
    item: FeishuListedMessage, *, channel: str = "feishu"
) -> InboundMessage | None:
    if not item.message_id:
        return None
    if item.chat_type not in (None, "", "p2p"):
        return None
    if item.msg_type != "text":
        return None
    sender = item.sender
    if sender is None or sender.sender_type != "user":
        return None
    try:
        raw_body = (item.body.content if item.body is not None else "") or "{}"
        content = str(_OptionalTextContent.model_validate_json(raw_body).text).strip()
    except ValueError:
        return None
    if not content:
        return None
    open_id = sender_open_id(sender)
    if not open_id:
        return None
    message_id = item.message_id
    return InboundMessage(
        channel=channel,
        chat_id=open_id,
        text=content,
        message_id=message_id,
        idempotency_key=cursors.idempotency_key(message_id),
    )
