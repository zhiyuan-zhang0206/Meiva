# meiva:link gateway/agents/history/docs/epoch-timeline.md

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from base.agents.epochs.model import HarnessEpoch
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.kwargs import NoteTag
from gateway.message_admission.cursor import (
    SWITCH_BLOCK_ID,
    UNAVAILABLE_ID,
    CursorPart,
    TimelineCursor,
    epoch_item_id,
)

__all__ = [
    "EpochPage",
    "EpochRead",
    "PageTarget",
    "Unavailable",
    "assemble_page",
    "page_target",
    "switch_block",
    "unavailable_marker",
    "usable_epochs",
]

MIN_EPOCHS = 2


class Unavailable(StrEnum):
    MACHINE_OFFLINE = "the agent's machine is offline"
    READ_FAILED = "the agent's machine could not read it"
    NO_SESSION = "no session record was kept"
    FILE_MISSING = "its record file was removed or moved"
    SESSION_REPLACED = "its session was replaced"
    UNDECODABLE = "its record cannot be decoded"
    UNSUPPORTED = "its harness keeps no readable record"


@dataclass(frozen=True)
class EpochRead:
    items: list[TimelineItem]
    has_more: bool
    msg_count: int = 0
    reading_source: str | None = None
    unavailable: Unavailable | None = None


@dataclass(frozen=True)
class EpochPage:
    items: list[TimelineItem]
    has_more: bool
    msg_count: int
    reading_source: str | None


@dataclass(frozen=True)
class PageTarget:
    seq: int | None
    within: TimelineCursor | None
    fixed: list[TimelineItem] | None = None
    fixed_has_more: bool = False


def usable_epochs(rows: Sequence[HarnessEpoch], harness: str) -> list[HarnessEpoch]:
    if len(rows) < MIN_EPOCHS:
        return []
    if [row.seq for row in rows] != list(range(len(rows))):
        return []
    if rows[-1].ended_at is not None or rows[-1].harness != harness:
        return []
    return list(rows)


def switch_block(epoch: HarnessEpoch, previous: HarnessEpoch) -> TimelineItem:
    model = f" ({epoch.model})" if epoch.model else ""
    reason = f"\n\nRestart message: {epoch.reason}" if epoch.reason else ""
    return _marker(
        epoch_item_id(epoch.seq, SWITCH_BLOCK_ID),
        NoteTag.HARNESS_SWITCH,
        f"Switched harness: {previous.harness} -> {epoch.harness}{model}{reason}",
        epoch.started_at,
    )


def unavailable_marker(epoch: HarnessEpoch, reason: Unavailable, *, current: bool) -> TimelineItem:
    which = "This session" if current else "Earlier session"
    return _marker(
        epoch_item_id(epoch.seq, UNAVAILABLE_ID),
        NoteTag.HISTORY_UNAVAILABLE,
        f"{which} on {epoch.harness} is unavailable: {reason.value}.",
        epoch.started_at,
    )


def _marker(item_id: str, tag: NoteTag, payload: str, at: datetime) -> TimelineItem:
    return TimelineItem(
        item_id=item_id,
        kind="system_marker",
        source=tag.value,
        payload=payload,
        created_at=at.isoformat(),
    )


def page_target(
    epochs: Sequence[HarnessEpoch], before: str | None, cursor: TimelineCursor | None
) -> PageTarget:
    current = len(epochs) - 1
    if before is None:
        return PageTarget(current, None)
    if cursor is None:
        return PageTarget(None, None, fixed=[])
    if cursor.epoch is None:
        return PageTarget(current, cursor)
    seq = cursor.epoch
    if seq > current:
        return PageTarget(None, None, fixed=[])
    if cursor.part is CursorPart.SWITCH:
        return PageTarget(seq - 1 if seq > 0 else None, None, fixed=None if seq > 0 else [])
    if cursor.part is CursorPart.UNAVAILABLE:
        if seq == 0:
            return PageTarget(None, None, fixed=[])
        return PageTarget(
            None, None, fixed=[switch_block(epochs[seq], epochs[seq - 1])], fixed_has_more=True
        )
    return PageTarget(seq, cursor)


def assemble_page(epochs: Sequence[HarnessEpoch], seq: int, read: EpochRead) -> EpochPage:
    current = len(epochs) - 1
    epoch = epochs[seq]
    reached_head = read.unavailable is not None or not read.has_more or not read.items
    items = list(read.items)
    if seq != current:
        items = [
            item.model_copy(update={"item_id": epoch_item_id(seq, item.item_id)}) for item in items
        ]
    if read.unavailable is not None:
        items = [unavailable_marker(epoch, read.unavailable, current=seq == current)]
    if reached_head and seq > 0:
        items = [switch_block(epoch, epochs[seq - 1]), *items]
    return EpochPage(
        items=items,
        has_more=not reached_head or seq > 0,
        msg_count=read.msg_count if seq == current and read.unavailable is None else 0,
        reading_source=read.reading_source if seq == current else None,
    )
