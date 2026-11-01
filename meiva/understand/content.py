# meiva:note
# What a `paths` target sends to its model: each file read as a part, the content types the parts
# carry, and the content list a given model receives. A media part becomes the provider-native
# block the shared attach packing (`base.lm.attach.packing`) builds for the model, so a model
# receives media in the shape its provider decodes.
# meiva:link meiva/understand/docs/understand.meiva.okf.md

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from base.lm.attach.constants import media_type_for_mime
from base.lm.attach.packing import file_size_error, media_block
from base.lm.catalog import ModelCatalog
from meiva.understand.errors import UnderstandError

__all__ = ["MediaPart", "Part", "TextPart", "content_for", "content_types"]


# meiva:note
# One file of a `paths` target as read: a text file's UTF-8 content, or a media file's bytes with
# its MIME type and the path it was named by (for refusals).
@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class MediaPart:
    path: str
    mime: str
    data: bytes

    @property
    def media_type(self) -> str:
        return media_type_for_mime(self.mime)


type Part = TextPart | MediaPart


# meiva:note
# The content types a target's parts carry: `text` for a text file, the media type otherwise.
def content_types(parts: Sequence[Part]) -> frozenset[str]:
    return frozenset("text" if isinstance(p, TextPart) else p.media_type for p in parts)


# meiva:note
# The content list `model` receives: the parts in `paths` order, the prompt last. A media part
# becomes the provider-native block the attach packing builds for `model`, after the same
# per-file size limits (the shared ceiling and the provider's own limit for the type).
#
# Raises:
#     UnderstandError: a media file is over a size limit for `model`.
def content_for(
    catalog: ModelCatalog, model: str, parts: Sequence[Part], prompt: str
) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    for part in parts:
        if isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.text})
            continue
        if reason := file_size_error(model, part.media_type, len(part.data), catalog=catalog):
            raise UnderstandError(f"{part.path!r} cannot be sent to {model}: {reason}")
        blocks.append(media_block(model, part.media_type, part.mime, part.data, catalog=catalog))
    blocks.append({"type": "text", "text": prompt})
    return blocks
