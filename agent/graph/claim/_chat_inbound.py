"""Build the HumanMessage for a kind='chat' inbound row.

Split out of `claim/node.py` so the multimodal image-inlining path lives in one
focused place: text inbounds envelope-wrap into a string message; multimodal
inbounds wrap the text part into a leading block and inline each referenced
image upload as a native base64 block the model decodes.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeGuard

from langchain_core.messages import HumanMessage

from agent.db import ClaimedInbound
from agent.messages import InboundTimes, inbound_message
from base.agents.messages.envelope import inbound_head, inbound_tail
from base.agents.messages.image_dimensions import (
    image_dimensions_from_bytes,
    image_dimensions_from_data_uri,
)
from base.agents.messages.inbound_images import (
    image_mime_for,
    inbound_upload_files,
    parse_upload_url,
)
from base.agents.messages.quote import payload_quote, quoted_context
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.agents.upload_delivery.paths import UploadClient, fetch_upload_b64
from base.host.boundary.fields import str_field
from base.host.boundary.json_values import is_object_dict
from base.log import logger


@dataclass(frozen=True, slots=True)
class _DeliveredImageUpload:
    name: str
    size: int
    content_type: str
    url: str
    path: str | None
    width: int | None = None
    height: int | None = None


def _nonneg_int(val: object) -> int | None:
    if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
        return val
    return None


def _pos_int(val: object) -> int | None:
    if isinstance(val, int) and not isinstance(val, bool) and val > 0:
        return val
    return None


def _parse_single_image_upload(agent_id: int, raw: object) -> _DeliveredImageUpload | None:
    if not _is_object_dict(raw):
        return None
    name, content_type, batch_id = raw.get("name"), raw.get("content_type"), raw.get("batch_id")
    size, ordinal, path = (
        _nonneg_int(raw.get("size")),
        _nonneg_int(raw.get("ordinal")),
        raw.get("path"),
    )
    if (
        not isinstance(name, str)
        or not isinstance(content_type, str)
        or not isinstance(batch_id, str)
        or len(batch_id) != 32
        or size is None
        or ordinal is None
        or (path is not None and not isinstance(path, str))
    ):
        return None
    mime = image_mime_for(name, content_type) if name else None
    if mime is None:
        return None
    width, height = _pos_int(raw.get("width")), _pos_int(raw.get("height"))
    if width is None or height is None:
        width, height = None, None
    return _DeliveredImageUpload(
        name=name,
        size=size,
        content_type=mime,
        url=f"/api/agents/{agent_id}/uploads/{batch_id}/{ordinal}",
        path=path,
        width=width,
        height=height,
    )


def _parse_image_uploads(
    agent_id: int, payload: Mapping[str, object] | None
) -> list[_DeliveredImageUpload]:
    raw_images = payload.get("image_uploads") if payload else None
    if not _is_object_list(raw_images):
        return []
    return [
        parsed
        for raw in raw_images
        if (parsed := _parse_single_image_upload(agent_id, raw)) is not None
    ]


def _read_image_upload_bytes(
    img: _DeliveredImageUpload, *, gateway: Callable[[], UploadClient]
) -> bytes | None:
    if img.path:
        local_path = Path(img.path)
        if local_path.is_file():
            return local_path.read_bytes()
    try:
        resp = gateway().get(img.url, timeout=15.0)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.warning("claim: delivered image upload {!r} unavailable: {!r}", img.name, e)
        return None


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_object_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _attach_upload_files(
    message: HumanMessage, agent_id: int, payload: Mapping[str, object] | None
) -> None:
    upload_files = inbound_upload_files(agent_id, payload)
    if upload_files:
        message.additional_kwargs["meiva_upload_files"] = [
            file.model_dump(exclude_none=True) for file in upload_files
        ]


def _attach_image_dimensions(
    message: HumanMessage, image_dimensions: list[dict[str, int] | None]
) -> None:
    if any(entry is not None for entry in image_dimensions):
        message.additional_kwargs["meiva_image_dimensions"] = image_dimensions


def _build_chat_inbound_with_uploads(
    item: ClaimedInbound,
    image_uploads: list[_DeliveredImageUpload],
    *,
    picked_up_at: datetime,
    envelope_inputs: TimestampPolicy,
    expand_command: Callable[[str], str],
    gateway: Callable[[], UploadClient],
) -> HumanMessage:
    raw_text = expand_command(item.content)
    head = inbound_head(item.source, created_at=item.created_at, inputs=envelope_inputs)
    content: list[dict[str, object]] = [
        {"type": "text", "text": head + raw_text + inbound_tail(item.source)}
    ]
    image_urls: list[str] = []
    image_dimensions: list[dict[str, int] | None] = []
    for img in image_uploads:
        image_urls.append(img.url)
        raw_bytes = _read_image_upload_bytes(img, gateway=gateway)
        dims: tuple[int, int] | None = (
            (img.width, img.height)
            if img.width is not None and img.height is not None
            else (image_dimensions_from_bytes(raw_bytes) if raw_bytes is not None else None)
        )
        image_dimensions.append({"width": dims[0], "height": dims[1]} if dims is not None else None)
        if raw_bytes is not None:
            b64 = base64.standard_b64encode(raw_bytes).decode("ascii")
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{img.content_type};base64,{b64}"}}
            )
    message = inbound_message(
        content=content,
        source=item.source,
        inbound_id=item.id,
        body_start=len(head),
        times=InboundTimes(arrived_at=item.created_at, picked_up_at=picked_up_at),
        image_urls=image_urls,
    )
    _attach_image_dimensions(message, image_dimensions)
    message.additional_kwargs["meiva_image_uploads"] = [
        {
            "name": img.name,
            "size": img.size,
            "content_type": img.content_type,
            "url": img.url,
            "path": img.path,
        }
        for img in image_uploads
    ]
    _attach_upload_files(message, item.agent_id, item.payload)
    return message


def _build_chat_inbound_from_blocks(
    item: ClaimedInbound,
    raw_blocks: list[object],
    *,
    picked_up_at: datetime,
    envelope_inputs: TimestampPolicy,
    expand_command: Callable[[str], str],
    gateway: Callable[[], UploadClient],
) -> HumanMessage:
    blocks: list[dict[str, object]] = [
        {str(k): v for k, v in b.items()} for b in raw_blocks if _is_object_dict(b)
    ]
    text = "\n".join(str(b["text"]) for b in blocks if b.get("type") == "text")
    raw_text = expand_command(text)
    head = inbound_head(item.source, created_at=item.created_at, inputs=envelope_inputs)
    content: list[dict[str, object]] = [
        {"type": "text", "text": head + raw_text + inbound_tail(item.source)}
    ]
    image_urls: list[str] = []
    image_dimensions: list[dict[str, int] | None] = []
    for b in blocks:
        if b.get("type") != "image_url":
            continue
        image_url = b.get("image_url")
        if not is_object_dict(image_url):
            raise TypeError(f"content image_url block must carry an image_url object, got {b!r}")
        url = str_field(image_url, "url")
        parsed = parse_upload_url(url)
        if parsed is None:
            content.append({"type": "text", "text": f"[image reference {url!r} unreadable]"})
            continue
        ref_agent, name = parsed
        try:
            mime, b64 = fetch_upload_b64(ref_agent, name, client=gateway)
        except (OSError, ValueError) as e:
            logger.warning(
                "claim: image upload {!r} unavailable (agent {}): {!r}", name, item.agent_id, e
            )
            content.append({"type": "text", "text": f"[image {name} unavailable]"})
            continue
        # meiva:note
        # Use standard image_url format (data URI) so the block is
        # decodable by Claude, Gemini, and GPT providers alike.
        # Anthropic-native {"type":"image","source":{...}} only works
        # with langchain-anthropic; langchain-google-genai and
        # langchain-openai expect {"type":"image_url","image_url":{...}}.
        data_uri = f"data:{mime};base64,{b64}"
        content.append({"type": "image_url", "image_url": {"url": data_uri}})
        image_urls.append(url)
        dims = image_dimensions_from_data_uri(data_uri)
        image_dimensions.append({"width": dims[0], "height": dims[1]} if dims is not None else None)
    message = inbound_message(
        content=content,
        source=item.source,
        inbound_id=item.id,
        body_start=len(head),
        times=InboundTimes(arrived_at=item.created_at, picked_up_at=picked_up_at),
        image_urls=image_urls,
    )
    _attach_image_dimensions(message, image_dimensions)
    return message


# meiva:note
# Build the HumanMessage for a kind='chat' inbound row.
#
#     Plain text (no content_blocks payload): a `/<name> ...` composer message is
#     expanded here — every command in it, in the order typed — into each
#     command's template + the user's note, then envelope-wrapped into a string
#     message. Multimodal (payload carries content_blocks):
#     the text part is wrapped into a leading text block, then each referenced
#     image upload is read off disk and inlined as a native base64 image block the
#     model decodes; the reference urls ride on additional_kwargs for the timeline.
#
#     An image that cannot be read (deleted between send and claim, or a split
#     deployment where the upload lives on the gateway not this runner) degrades to
#     a "[image unavailable]" text note rather than crashing the claim — the miss
#     is logged, the rest of the message still delivers.
def _build_chat_inbound(
    item: ClaimedInbound,
    *,
    picked_up_at: datetime,
    envelope_inputs: TimestampPolicy,
    expand_command: Callable[[str], str],
    gateway: Callable[[], UploadClient],
) -> HumanMessage:
    raw_blocks = item.payload.get("content_blocks") if item.payload else None
    image_uploads = _parse_image_uploads(item.agent_id, item.payload)
    if image_uploads:
        return _build_chat_inbound_with_uploads(
            item,
            image_uploads,
            picked_up_at=picked_up_at,
            envelope_inputs=envelope_inputs,
            expand_command=expand_command,
            gateway=gateway,
        )
    if not _is_object_list(raw_blocks):
        raw = expand_command(item.content)
        head = inbound_head(item.source, created_at=item.created_at, inputs=envelope_inputs)
        message = inbound_message(
            content=head + raw + inbound_tail(item.source),
            source=item.source,
            inbound_id=item.id,
            body_start=len(head),
            times=InboundTimes(arrived_at=item.created_at, picked_up_at=picked_up_at),
        )
        _attach_upload_files(message, item.agent_id, item.payload)
        return message
    return _build_chat_inbound_from_blocks(
        item,
        raw_blocks,
        picked_up_at=picked_up_at,
        envelope_inputs=envelope_inputs,
        expand_command=expand_command,
        gateway=gateway,
    )


def build_chat_inbound(
    item: ClaimedInbound,
    *,
    picked_up_at: datetime,
    envelope_inputs: TimestampPolicy,
    expand_command: Callable[[str], str],
    gateway: Callable[[], UploadClient],
) -> HumanMessage:
    # meiva:note
    # Render quote context after command expansion so quoted commands stay inert.
    quote = payload_quote(item.payload)
    message = _build_chat_inbound(
        item,
        picked_up_at=picked_up_at,
        envelope_inputs=envelope_inputs,
        expand_command=lambda text: quoted_context(expand_command(text), quote),
        gateway=gateway,
    )
    if quote is not None:
        message.additional_kwargs["meiva_quote"] = quote.model_dump()
        message.additional_kwargs["meiva_quote_body"] = item.content
    return message
