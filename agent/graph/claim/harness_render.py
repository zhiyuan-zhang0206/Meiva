from __future__ import annotations

import base64
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from agent import state as _state
from base.agents.messages.kwargs import read_meiva_kwargs
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.content import message_content
from base.lm.harness import ImageBlock


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


# meiva:note
# The size bounds of an image handed to an external CLI as an image, not as a path. One image is bounded by
# the smallest per-image limit of the three providers behind the CLIs (the Anthropic API takes at most
# 5 MB per image); the images of one input, the merged input of a whole wake, together are bounded by
# what fits a provider's request once base64 inflates them (Gemini's inline request limit is 20 MB, so
# 15 MB of raw bytes). An image over either bound is not sent inline; the text says so and names the
# path it already lists.
# meiva:link agent/graph/docs/harness-foreground.md
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_INPUT_IMAGE_BYTES = 15 * 1024 * 1024
_DATA_URL = "data:"


# meiva:note
# The images a message carries as native content (the `image_url` data-URL blocks the claim inlined from
# the uploads), as `ImageBlock`s in order, and how many were left out for being over the per-image bound.
# The per-input total is not judged here: a wake's rows are one input, so `fit_input_images` judges it
# over all their images. A data URL that is not a base64 image of a supported type raises: the claim
# only writes those.
def native_images(messages: Sequence[BaseMessage]) -> tuple[tuple[ImageBlock, ...], int]:
    images: list[ImageBlock] = []
    skipped = 0
    for message in messages:
        content = message_content(message)
        if isinstance(content, str):
            continue
        for block in content:
            if isinstance(block, str) or block.get("type") != "image_url":
                continue
            image_url = block.get("image_url")
            url = image_url.get("url") if is_object_dict(image_url) else None
            if not isinstance(url, str) or not url.startswith(_DATA_URL):
                raise ValueError("an inlined image block must carry a data URL")
            head, _, data = url.partition(",")
            mime, _, encoding = head[len(_DATA_URL) :].partition(";")
            if encoding != "base64":
                raise ValueError(f"an inlined image must be base64, got {encoding!r}")
            if len(base64.b64decode(data, validate=True)) > MAX_IMAGE_BYTES:
                skipped += 1
                continue
            images.append(ImageBlock(mime_type=mime, data_base64=data))
    return tuple(images), skipped


# meiva:note
# The images of one input that fit its total bound, row by row in order, and how many were left out.
# An image that does not fit is left out and a later, smaller one may still fit.
def fit_input_images(
    rows: Sequence[Sequence[ImageBlock]],
) -> tuple[tuple[tuple[ImageBlock, ...], ...], int]:
    total = 0
    skipped = 0
    kept: list[tuple[ImageBlock, ...]] = []
    for images in rows:
        fits: list[ImageBlock] = []
        for image in images:
            size = len(base64.b64decode(image.data_base64, validate=True))
            if total + size > MAX_INPUT_IMAGE_BYTES:
                skipped += 1
                continue
            total += size
            fits.append(image)
        kept.append(tuple(fits))
    return tuple(kept), skipped


def skipped_images_note(skipped: int) -> str:
    noun = "image was" if skipped == 1 else "images were"
    return (
        f"{skipped} attached {noun} not sent inline because of its size (limit "
        f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB per image, {MAX_INPUT_IMAGE_BYTES // (1024 * 1024)} MB "
        "per input); open the file at the path listed above."
    )
