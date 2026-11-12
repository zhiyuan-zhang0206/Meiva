from pydantic import BaseModel, ConfigDict, Field

from base.agents.history.timeline_item import TimelineItem


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
    explicit_visibility: bool
    delivery_mode: str | None


# meiva:note
# `size` is the record file's size in bytes, the exclusive upper bound of every item's offset (the timeline's
# `msg_count`); a session with no record yet, or whose file is not written yet, is an empty window with size 0.
class HarnessTimelineWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[TimelineItem]
    has_more: bool
    size: int = Field(ge=0)
