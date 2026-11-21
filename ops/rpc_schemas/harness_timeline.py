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
    reading_locator: str | None = Field(default=None, max_length=MAX_READING_LOCATOR_LENGTH)
    reading_direction: ReadingDirection | None = None
    explicit_visibility: bool
    delivery_mode: str | None

    @model_validator(mode="after")
    def validate_selector(self) -> Self:
        selectors = sum(
            value is not None for value in (self.before, self.anchor_item_id, self.reading_locator)
        )
        if selectors > 1:
            raise ValueError("A record window requires one exclusive or inclusive selector")
        if (self.reading_locator is None) != (self.reading_direction is None):
            raise ValueError("A reading locator and direction are required together")
        if self.reading_locator is not None:
            _ = decode_reading_locator(self.reading_locator)
        return self

    def wire_payload(self) -> dict[str, object]:
        selectors = {
            "anchor_item_id": self.anchor_item_id,
            "reading_locator": self.reading_locator,
            "reading_direction": self.reading_direction,
        }
        omitted = {name for name, value in selectors.items() if value is None}
        return self.model_dump(mode="json", exclude=omitted)


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
    reading_source: str | None = Field(default=None, exclude_if=_missing_reading_metadata)
    reading_applied: bool = Field(default=False, exclude_if=_missing_reading_metadata)
    reading_unavailable: ReadingUnavailableReason | None = Field(
        default=None, exclude_if=_missing_reading_metadata
    )
    older_cursor: str | None = Field(default=None, exclude_if=_missing_reading_metadata)
    newer_cursor: str | None = Field(default=None, exclude_if=_missing_reading_metadata)
