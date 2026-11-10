from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass

# meiva:note
# One input handed to a harness foreground: our own id for it (`key`), its text and its images.
# The key is what every later event names, so it must be unique per input and stable across a
# re-delivery. Validation happens here, once, at the boundary: an adapter never sees an input that
# has no key, no content, or an image the protocol cannot carry, and it never repairs one.
# meiva:link base/lm/harness/docs/delivery.md
IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


@dataclass(frozen=True, slots=True)
class ImageBlock:
    mime_type: str
    data_base64: str

    def __post_init__(self) -> None:
        if self.mime_type not in IMAGE_MIME_TYPES:
            raise ValueError(f"unsupported image type {self.mime_type!r}")
        try:
            decoded = base64.b64decode(self.data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("image data is not valid base64") from exc
        if not decoded:
            raise ValueError("image data is empty")


@dataclass(frozen=True, slots=True)
class HarnessInput:
    key: str
    text: str
    images: tuple[ImageBlock, ...] = ()

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("an input needs a key")
        if not self.text.strip() and not self.images:
            raise ValueError("an input needs text or an image")


# meiva:note
# A batch is delivered in order and its keys name its members, so a repeated key would make two
# inputs indistinguishable in every later event.
def validated_batch(inputs: Sequence[HarnessInput]) -> tuple[HarnessInput, ...]:
    batch = tuple(inputs)
    if not batch:
        raise ValueError("a delivery needs at least one input")
    keys = [item.key for item in batch]
    if len(set(keys)) != len(keys):
        raise ValueError("input keys in one delivery must be unique")
    return batch
