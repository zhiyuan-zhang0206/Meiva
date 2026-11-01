# meiva:note
# Pure packing of registered local files into provider-native media blocks.
#
# `base.lm.factory` owns tiered media-capability resolution so packing and message-endpoint image
# gating use the same core, plugin, and prefix answers.
#
# The packing never touches a file. `plan_attachments` is a generator that yields the observation
# it needs next (a stat, an image's dimensions, a file's bytes) and receives what the world
# answered; `base.lm.attach.files.pack_attachments` runs it against the filesystem.

from __future__ import annotations

import base64
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from base.lm.attach.constants import (
    ATTACH_MAX_FILE_BYTES,
    ATTACH_MAX_FILES_PER_TURN,
    ATTACH_MAX_LABEL_CHARS,
    ATTACH_MAX_TOTAL_BYTES,
    ATTACH_MEDIA_MIME,
    media_type_for_mime,
)
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_contract import AttachPolicy
from base.lm.registry import attach_modalities_for_model
from base.lm.system_note import wrap_system_note

# Keep the notice bare: attachments persist in the message history, so any
# extra instruction is redundant copy (user ruling 2026-08-30).
_ATTACH_NOTICE = "Files attached during this turn:"


@dataclass(frozen=True)
class AttachEntry:
    """One attachment whose path was resolved when it was registered."""

    path: str
    label: str | None


@dataclass(frozen=True)
class AttachmentPack:
    """Caption plus native content blocks ready for one HumanMessage."""

    blocks: list[dict[str, object]]
    text: str
    delivered: list[str]
    skipped: list[tuple[str, str]]


# meiva:note
# What the packing asks the world for, one observation at a time. `StatRequest` resolves an
# entry's path and stats it; the other two name the resolved path the stat answered with.
@dataclass(frozen=True)
class StatRequest:
    entry_path: str


@dataclass(frozen=True)
class DimensionsRequest:
    path: str


@dataclass(frozen=True)
class ReadRequest:
    path: str


type PackRequest = StatRequest | DimensionsRequest | ReadRequest


# meiva:note
# What the world answered. `outcome` says how far the stat got: the entry's path did not resolve,
# the file is missing, it could not be stat-ed, or the stat succeeded (`is_regular` and `size`
# then describe it). `path` is the resolved path text, or the entry's own text when it did not
# resolve. A dimensions or read answer carries None when the file could not be read.
@dataclass(frozen=True)
class StatObserved:
    outcome: Literal["invalid_path", "missing", "unstatable", "stat"]
    path: str
    is_regular: bool = False
    size: int | None = None


@dataclass(frozen=True)
class DimensionsObserved:
    dimensions: tuple[int, int] | None


@dataclass(frozen=True)
class ReadObserved:
    data: bytes | None


type Observation = StatObserved | DimensionsObserved | ReadObserved


@dataclass(frozen=True)
class _AttachmentFile:
    """A regular recognized file after its pack-time stat check."""

    path_text: str
    size: int
    mime: str
    media_type: str


@dataclass(frozen=True)
class _SkippedAttachment:
    """A file that cannot reach the model, retained for the text caption."""

    path: str
    mime: str | None
    size: int | None
    reason: str


def _new_media_blocks() -> list[dict[str, object]]:
    return []


def _new_delivered_paths() -> list[str]:
    return []


def _new_skipped_paths() -> list[tuple[str, str]]:
    return []


def _new_delivered_flags() -> list[bool]:
    return []


class _PackingState:
    """The ordered pack output while files are accepted or skipped.

    ``line_blocks`` / ``delivered_flags`` run in ENTRY order (one text block and
    one flag per entry, delivered or skipped) so the final content blocks can
    interleave each entry's caption line with its media block — the model sees
    every file's label right beside its image, and the frontend timeline can
    pair each delivered image with its own caption line structurally.
    """

    def __init__(self) -> None:
        self.media_blocks = _new_media_blocks()
        self.caption_lines: list[str] = [wrap_system_note(_ATTACH_NOTICE)]
        self.line_blocks = _new_media_blocks()
        self.delivered_flags = _new_delivered_flags()
        self.delivered = _new_delivered_paths()
        self.skipped = _new_skipped_paths()
        self.delivered_bytes = 0
        self.delivered_images = 0

    def skip(self, index: int, label: str | None, attachment: _SkippedAttachment) -> None:
        self.skipped.append((attachment.path, attachment.reason))
        line = _caption_line(
            index,
            attachment.path,
            attachment.mime,
            attachment.size,
            label,
            attachment.reason,
        )
        self.caption_lines.append(line)
        self.line_blocks.append({"type": "text", "text": line})
        self.delivered_flags.append(False)

    def deliver(
        self,
        model: str,
        index: int,
        label: str | None,
        attachment: _AttachmentFile,
        data: bytes,
        *,
        catalog: ModelCatalog,
    ) -> None:
        self.media_blocks.append(
            media_block(model, attachment.media_type, attachment.mime, data, catalog=catalog)
        )
        self.delivered.append(attachment.path_text)
        self.delivered_bytes = self.delivered_bytes + len(data)
        if attachment.media_type == "image":
            self.delivered_images = self.delivered_images + 1
        line = _caption_line(index, attachment.path_text, attachment.mime, len(data), label, None)
        self.caption_lines.append(line)
        self.line_blocks.append({"type": "text", "text": line})
        self.delivered_flags.append(True)


# meiva:note
# Pack current attachment files into content blocks without raising on bad files.
#
# Uses the attach contract (`attach_modalities_for_model`) rather than the raw
# endpoint matrix so a file `attach()` rejected never slips through as a caption.
# The generator yields the observations it needs, in the order the checks need them (a
# file is read only after the cheaper checks passed), and returns the pack.
def plan_attachments(
    model: str,
    entries: list[AttachEntry],
    *,
    catalog: ModelCatalog,
    max_files_per_turn: int = ATTACH_MAX_FILES_PER_TURN,
) -> Generator[PackRequest, Observation, AttachmentPack | None]:
    if not entries:
        return None

    supported_media_types = attach_modalities_for_model(
        model,
        models=catalog.models,
        vision_prefixes={prefix: binding.vision for prefix, binding in catalog.bindings.items()},
    )
    state = _PackingState()

    for index, entry in enumerate(entries, start=1):
        stat_answer = yield StatRequest(entry.path)
        attachment = _attachment_file(_expect_stat(stat_answer))
        if isinstance(attachment, _SkippedAttachment):
            state.skip(index, entry.label, attachment)
            continue
        if attachment.media_type not in supported_media_types:
            state.skip(
                index,
                entry.label,
                _skip_for(attachment, f"your model cannot receive {attachment.media_type}"),
            )
            continue
        if reason := _delivery_error(
            model,
            state,
            attachment.size,
            attachment.media_type,
            catalog=catalog,
            max_files_per_turn=max_files_per_turn,
        ):
            state.skip(index, entry.label, _skip_for(attachment, reason))
            continue
        if attachment.media_type == "image":
            dimensions_answer = yield DimensionsRequest(attachment.path_text)
            reason = _image_dimension_error(
                model,
                state.delivered_images,
                _expect_dimensions(dimensions_answer),
                catalog=catalog,
            )
            if reason:
                state.skip(index, entry.label, _skip_for(attachment, reason))
                continue
        read_answer = yield ReadRequest(attachment.path_text)
        data = _expect_read(read_answer).data
        if data is None:
            state.skip(index, entry.label, _skip_for(attachment, "cannot read file"))
            continue
        if reason := _delivery_error(
            model,
            state,
            len(data),
            attachment.media_type,
            catalog=catalog,
            max_files_per_turn=max_files_per_turn,
        ):
            state.skip(index, entry.label, _skip_for(attachment, reason, len(data)))
            continue
        state.deliver(model, index, entry.label, attachment, data, catalog=catalog)

    text = "\n".join(state.caption_lines)
    # meiva:note
    # Interleave each entry's caption line with its media block, behind the leading notice:
    # [text(notice), text(line1), media1, text(line2), media2, ...]. Every delivered media
    # block is immediately preceded by its own caption text block, so consumers pair a file's
    # label with its image without re-parsing the joined caption
    # (base/agents/history/timeline._attach_image_captions). The notice is one complete
    # system note inside its own block — the captions interleave with media, so a note
    # spanning them would move blocks; block count and order stay as they are.
    # meiva:link agent/messages/docs/message-format.meiva.okf.md
    blocks: list[dict[str, object]] = [{"type": "text", "text": state.caption_lines[0]}]
    media_index = 0
    for line_block, delivered_flag in zip(state.line_blocks, state.delivered_flags, strict=True):
        blocks.append(line_block)
        if delivered_flag:
            blocks.append(state.media_blocks[media_index])
            media_index += 1
    return AttachmentPack(
        blocks=blocks,
        text=text,
        delivered=state.delivered,
        skipped=state.skipped,
    )


# meiva:note
# The answers arrive typed as the union; a driver that answers a request with another kind of
# observation is a bug in the driver, not a bad file.
def _expect_stat(answer: Observation) -> StatObserved:
    if not isinstance(answer, StatObserved):
        raise TypeError(f"a stat request was answered with {type(answer).__name__}")
    return answer


def _expect_dimensions(answer: Observation) -> DimensionsObserved:
    if not isinstance(answer, DimensionsObserved):
        raise TypeError(f"a dimensions request was answered with {type(answer).__name__}")
    return answer


def _expect_read(answer: Observation) -> ReadObserved:
    if not isinstance(answer, ReadObserved):
        raise TypeError(f"a read request was answered with {type(answer).__name__}")
    return answer


# meiva:note
# Turn what the stat found into the file to pack or the skip to caption: an unresolvable,
# missing, unstatable, non-regular, or unrecognized file becomes a captioned skip.
def _attachment_file(observed: StatObserved) -> _AttachmentFile | _SkippedAttachment:
    match observed.outcome:
        case "invalid_path":
            return _SkippedAttachment(observed.path, None, None, "invalid file path")
        case "missing":
            return _SkippedAttachment(observed.path, None, None, "file does not exist")
        case "unstatable":
            return _SkippedAttachment(observed.path, None, None, "cannot stat file")
        case "stat":
            pass
    size = observed.size
    if size is None:
        raise ValueError("a successful stat carries a size")
    if not observed.is_regular:
        return _SkippedAttachment(observed.path, None, size, "not a regular file")
    path = Path(observed.path)
    mime = ATTACH_MEDIA_MIME.get(path.suffix.lower())
    if mime is None:
        return _SkippedAttachment(observed.path, None, size, "unknown media suffix")
    return _AttachmentFile(
        path_text=observed.path,
        size=size,
        mime=mime,
        media_type=media_type_for_mime(mime),
    )


def _skip_for(
    attachment: _AttachmentFile, reason: str, size: int | None = None
) -> _SkippedAttachment:
    attachment_size = attachment.size if size is None else size
    return _SkippedAttachment(attachment.path_text, attachment.mime, attachment_size, reason)


def _delivery_error(
    model: str,
    state: _PackingState,
    size: int,
    media_type: str,
    *,
    catalog: ModelCatalog,
    max_files_per_turn: int = ATTACH_MAX_FILES_PER_TURN,
) -> str | None:
    if reason := file_size_error(model, media_type, size, catalog=catalog):
        return reason
    if len(state.delivered) >= max_files_per_turn:
        return f"turn already has {max_files_per_turn} delivered files"
    if state.delivered_bytes + size > ATTACH_MAX_TOTAL_BYTES:
        return f"would exceed {_mib(ATTACH_MAX_TOTAL_BYTES)} total limit"
    return None


# meiva:note
# Why one file of `size` bytes cannot reach `model` inline, or None when it can: the shared
# per-file ceiling, then the provider's own limit for the media type (`AttachPolicy`). Attach
# packing and `meiva.understand` both judge a file by it.
def file_size_error(model: str, media_type: str, size: int, *, catalog: ModelCatalog) -> str | None:
    if size > ATTACH_MAX_FILE_BYTES:
        return f"file exceeds {_mib(ATTACH_MAX_FILE_BYTES)} limit"
    provider_size_limit = _file_size_limit(model, media_type, catalog=catalog)
    if size > provider_size_limit:
        return f"file exceeds {_mib(provider_size_limit)} limit"
    return None


def _image_dimension_error(
    model: str, delivered_images: int, observed: DimensionsObserved, *, catalog: ModelCatalog
) -> str | None:
    if observed.dimensions is None:
        return "cannot read image dimensions"
    dimension_limit = _image_dimension_limit(model, delivered_images + 1, catalog=catalog)
    if max(observed.dimensions) > dimension_limit:
        return f"image exceeds {dimension_limit} px dimension limit"
    return None


def _file_size_limit(model: str, media_type: str, *, catalog: ModelCatalog) -> int:
    policy = _attach_policy(model, catalog=catalog)
    if policy is not None:
        return policy.file_size_limits.get(media_type, ATTACH_MAX_FILE_BYTES)
    return ATTACH_MAX_FILE_BYTES


def _attach_policy(model: str, *, catalog: ModelCatalog) -> AttachPolicy | None:
    for prefix, binding in catalog.bindings.items():
        if model.startswith(prefix):
            return binding.attach
    return None


def _image_dimension_limit(model: str, image_count: int, *, catalog: ModelCatalog) -> int:
    # Sentinel high (the int32 ceiling) until a model policy tier applies:
    # nothing constrains the dimension, and the value stays representable
    # everywhere it flows (task #3696 exception inventory; tiers below
    # override).
    dimension_limit = 2**31 - 1
    policy = _attach_policy(model, catalog=catalog)
    if policy is None:
        return dimension_limit
    for min_image_count, max_px in policy.image_dimension_tiers:
        if min_image_count > image_count:
            break
        dimension_limit = max_px
    return dimension_limit


# meiva:note
# The provider-native content block that carries one media file to `model`: an image as a
# standard `image_url` data URI (every provider client decodes it), a PDF as a `document` block
# where the provider's `AttachPolicy` asks for one, any other media as an inline `media` part.
# Attach packing and `meiva.understand` both build their media blocks here.
def media_block(
    model: str, media_type: str, mime: str, file_bytes: bytes, *, catalog: ModelCatalog
) -> dict[str, object]:
    if media_type == "image":
        encoded = base64.b64encode(file_bytes).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
    policy = _attach_policy(model, catalog=catalog)
    if media_type == "pdf" and policy is not None and policy.pdf_document_block:
        encoded = base64.b64encode(file_bytes).decode("ascii")
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
        }
    return {"type": "media", "mime_type": mime, "data": file_bytes}


def _caption_line(
    index: int,
    path: str,
    mime: str | None,
    size: int | None,
    label: str | None,
    reason: str | None,
) -> str:
    details = mime or "unknown"
    if size is not None:
        details = f"{details}, {_format_size(size)}"
    caption = f"- [{index}] {Path(path).name} ({details})"
    sanitized_label = _sanitize_label(label)
    if sanitized_label is not None:
        caption += f' — "{sanitized_label}"'
    if reason is not None:
        caption += f" — not delivered: {reason}"
    return caption


def _sanitize_label(label: str | None) -> str | None:
    if label is None:
        return None
    sanitized = "".join(char for char in label if char >= " " and char != "\x7f").strip()
    return sanitized[:ATTACH_MAX_LABEL_CHARS] or None


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    return f"{size / 1024:.1f} KiB"


def _mib(size: int) -> str:
    return f"{size // (1024 * 1024)} MiB"
