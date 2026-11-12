from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Sequence
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
class TextBlock:
    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("a text block needs text")


InputBlock = TextBlock | ImageBlock


# meiva:note
# What one input holds is an ordered sequence of blocks, text and images: a wake's messages each
# become a text block with the images that belong to the message right behind it. An adapter that
# can carry blocks (Claude Code, Codex) sends them as they are; one that cannot (Antigravity's stdin
# takes a single string and an image only as a separate RPC) reads the flattened views, `text` (the
# text blocks joined by a blank line) and `images` (every image, in order).
@dataclass(frozen=True, slots=True)
class HarnessInput:
    key: str
    blocks: tuple[InputBlock, ...]

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("an input needs a key")
        if not self.blocks:
            raise ValueError("an input needs text or an image")

    # meiva:note
    # One text block, then the images after it: the shape of a single message.
    @classmethod
    def of(cls, key: str, text: str, images: Sequence[ImageBlock] = ()) -> HarnessInput:
        lead = (TextBlock(text),) if text.strip() else ()
        return cls(key, (*lead, *images))

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if isinstance(b, TextBlock))

    @property
    def images(self) -> tuple[ImageBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ImageBlock))

    # meiva:note
    # The same input with its first text block rewritten (what rides in front of the first message:
    # the handoff, a reminder). An input that opens with an image gets the rewrite of "" as a text
    # block of its own in front.
    def with_lead_text(self, rewrite: Callable[[str], str]) -> HarnessInput:
        first = self.blocks[0]
        if isinstance(first, TextBlock):
            return HarnessInput(self.key, (TextBlock(rewrite(first.text)), *self.blocks[1:]))
        lead = rewrite("")
        return HarnessInput(self.key, (*((TextBlock(lead),) if lead.strip() else ()), *self.blocks))


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
