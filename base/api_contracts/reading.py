from __future__ import annotations

import base64
import binascii
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class ReadingDirection(StrEnum):
    ANCHOR = "anchor"
    BEFORE = "before"
    AFTER = "after"


class ReadingUnavailableReason(StrEnum):
    IDENTITY = "identity"
    SOURCE = "source"
    BUDGET = "budget"


class ReadingSourceUnavailableError(ValueError):
    def __init__(self, reason: ReadingUnavailableReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class RecordReadingSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    source: Literal["record"] = "record"
    agent_id: int = Field(strict=True, ge=1)
    harness: Literal["claude_code", "codex", "antigravity"]
    session_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")


class RecordReadingLocator(RecordReadingSource):
    offset: int = Field(strict=True, ge=0, le=2**63 - 1)
    block: int = Field(strict=True, ge=0, le=2**63 - 1)

    def item_id(self) -> str:
        return f"h{self.offset}.{self.block}"


class NativeReadingSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    source: Literal["native"] = "native"
    agent_id: int = Field(strict=True, ge=1)
    epoch: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")
    checkpoint_id: str | None = Field(
        default=None, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$"
    )


class NativeReadingLocator(NativeReadingSource):
    message_id: str | None = Field(
        default=None, min_length=1, max_length=512, pattern=r"^[^\x00]+$"
    )
    inbound_id: int | None = Field(default=None, strict=True, ge=1)
    block: int = Field(strict=True, ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def require_stable_identity(self) -> Self:
        if self.message_id is None and self.inbound_id is None:
            raise ValueError(
                "A native reading locator requires a stable message or inbound identity"
            )
        return self


ReadingLocator = Annotated[
    RecordReadingLocator | NativeReadingLocator, Field(discriminator="source")
]
MAX_READING_LOCATOR_LENGTH = 4096
INITIAL_READING_EPOCH = "initial"


def encode_reading_source(source: RecordReadingSource | NativeReadingSource) -> str:
    raw = source.model_dump_json(exclude_none=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def native_reading_epoch(checkpoint_id: str | None, boundaries: tuple[str, ...]) -> str:
    if checkpoint_id is None:
        return boundaries[0] if boundaries else INITIAL_READING_EPOCH
    if checkpoint_id not in boundaries:
        raise ReadingSourceUnavailableError(
            ReadingUnavailableReason.SOURCE, "The retained source is unavailable"
        )
    index = boundaries.index(checkpoint_id) + 1
    return boundaries[index] if index < len(boundaries) else INITIAL_READING_EPOCH


def native_reading_checkpoint(
    locator: NativeReadingSource, boundaries: tuple[str, ...], depth: int
) -> str | None:
    checkpoint = locator.checkpoint_id
    current_epoch = native_reading_epoch(None, boundaries)
    if checkpoint is None and locator.epoch != current_epoch:
        if locator.epoch == INITIAL_READING_EPOCH and boundaries:
            checkpoint = boundaries[-1]
        elif locator.epoch in boundaries and boundaries.index(locator.epoch) > 0:
            checkpoint = boundaries[boundaries.index(locator.epoch) - 1]
        else:
            raise ReadingSourceUnavailableError(
                ReadingUnavailableReason.SOURCE, "The native source domain is unavailable"
            )
    if checkpoint is not None and (
        checkpoint not in boundaries or (depth >= 0 and boundaries.index(checkpoint) + 1 > depth)
    ):
        raise ReadingSourceUnavailableError(
            ReadingUnavailableReason.SOURCE, "The retained source is outside the available history"
        )
    if native_reading_epoch(checkpoint, boundaries) != locator.epoch:
        raise ReadingSourceUnavailableError(
            ReadingUnavailableReason.SOURCE, "The native source domain changed"
        )
    return checkpoint


# meiva:note
# Source-issued coordinates are transport metadata, not authorization. Readers
# validate the current source and exact membership again after decoding.
# meiva:link base/agents/history/docs/timeline/timeline.md
def encode_reading_locator(locator: ReadingLocator) -> str:
    raw = locator.model_dump_json(exclude_none=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_reading_locator(value: str) -> ReadingLocator:
    if not value or len(value) > MAX_READING_LOCATOR_LENGTH or not value.isascii():
        raise ValueError("Invalid reading locator")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        locator = TypeAdapter[RecordReadingLocator | NativeReadingLocator](
            ReadingLocator
        ).validate_json(raw)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid reading locator") from exc
    if encode_reading_locator(locator) != value:
        raise ValueError("Invalid reading locator encoding")
    return locator
