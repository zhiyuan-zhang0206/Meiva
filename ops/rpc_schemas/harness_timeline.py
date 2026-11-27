from enum import StrEnum
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from base.agents.history.timeline_item import TimelineItem
from base.api_contracts.reading import (
    MAX_READING_LOCATOR_LENGTH,
    ReadingDirection,
    ReadingUnavailableReason,
    decode_reading_locator,
)


def _missing_reading_metadata(value: object) -> bool:
    return value is None or value is False


class RecordUnreadable(StrEnum):
    NO_SESSION = "no_session"
    FILE_MISSING = "file_missing"
    SESSION_MISMATCH = "session_mismatch"
    UNDECODABLE = "undecodable"


# meiva:note
# The gateway asks the machine an agent lives on for the newest `limit` timeline items of a harness whose record is
# read when asked (`before` is the byte offset of the item the window ends before, the number in an `h<offset>.<n>` item
# id). The reply policy is the agent's own (`explicit_visibility`, `delivery_mode`): the gateway knows it and the
# runner's operation reads no settings.
# meiva:link docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md
class HarnessTimelineReadRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: int = Field(ge=1)
    harness: str
    limit: int = Field(ge=1, le=1000)
    before: int | None = Field(default=None, ge=0)
    anchor_item_id: str | None = Field(default=None, pattern=r"^h[0-9]{1,20}\.[0-9]{1,20}$")
    quote_identity: str | None = Field(default=None, max_length=512)
    quote_inbound_id: int | None = Field(default=None, gt=0, lt=2**63)
    reading_locator: str | None = Field(default=None, max_length=MAX_READING_LOCATOR_LENGTH)
    reading_direction: ReadingDirection | None = None
    explicit_visibility: bool
    delivery_mode: str | None
    floor: int | None = Field(default=None, ge=0)
    session_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_selector(self) -> Self:
        if self.floor is not None and (
            self.anchor_item_id is not None or self.reading_locator is not None
        ):
            raise ValueError("A record floor applies to a plain or `before` window")
        if self.floor is not None and self.before is not None and self.before < self.floor:
            raise ValueError("A record window cannot end before its floor")
        selectors = sum(
            value is not None
            for value in (
                self.before,
                self.anchor_item_id,
                self.reading_locator,
                self.quote_identity,
            )
        )
        if selectors > 1:
            raise ValueError("A record window requires one exclusive or inclusive selector")
        if self.quote_inbound_id is not None and self.quote_identity is None:
            raise ValueError("A quote inbound id requires a quote identity")
        if (self.reading_locator is None) != (self.reading_direction is None):
            raise ValueError("A reading locator and direction are required together")
        if self.reading_locator is not None:
            _ = decode_reading_locator(self.reading_locator)
        return self

    def wire_payload(self) -> dict[str, object]:
        selectors = {
            "anchor_item_id": self.anchor_item_id,
            "quote_identity": self.quote_identity,
            "quote_inbound_id": self.quote_inbound_id,
            "reading_locator": self.reading_locator,
            "reading_direction": self.reading_direction,
            "floor": self.floor,
            "session_id": self.session_id,
        }
        omitted = {name for name, value in selectors.items() if value is None}
        return self.model_dump(mode="json", exclude=omitted)


class RecordQuoteEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str
    inbound_id: int | None
    activity_pending: bool
    created_at: str | None


# meiva:note
# `size` is the record file's size in bytes, the exclusive upper bound of every item's offset (the timeline's
# `msg_count`); a session with no record yet, or whose file is not written yet, is an empty window with size 0.
class HarnessTimelineWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[TimelineItem]
    has_more: bool
    size: int = Field(ge=0)
    anchor_item_id: str | None = None
    anchor_applied: bool = False
    quote_applied: bool = Field(default=False, exclude_if=_missing_reading_metadata)
    quote_evidence: RecordQuoteEvidence | None = Field(
        default=None, exclude_if=_missing_reading_metadata
    )
    reading_source: str | None = Field(default=None, exclude_if=_missing_reading_metadata)
    reading_applied: bool = Field(default=False, exclude_if=_missing_reading_metadata)
    reading_unavailable: ReadingUnavailableReason | None = Field(
        default=None, exclude_if=_missing_reading_metadata
    )
    unreadable: RecordUnreadable | None = Field(default=None, exclude_if=_missing_reading_metadata)
    older_cursor: str | None = Field(default=None, exclude_if=_missing_reading_metadata)
    newer_cursor: str | None = Field(default=None, exclude_if=_missing_reading_metadata)
