# meiva:link base/agents/epochs/docs/native-slice.md

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from base.agents.epochs.model import HarnessEpoch
from base.agents.history.native_source import effective_native_stamps
from base.agents.history.native_source import moment_of as moment_of
from base.agents.history.timeline_item import TimelineItem

NATIVE_HARNESS = "meiva"


@dataclass(frozen=True, slots=True)
class NativeEpochSlice:
    not_before: datetime | None
    before: datetime | None
    first: bool

    def holds(self, moment: datetime | None) -> bool:
        if moment is None:
            return self.first
        return (self.not_before is None or moment >= self.not_before) and (
            self.before is None or moment < self.before
        )


def native_slice_of(epochs: Sequence[HarnessEpoch], seq: int) -> NativeEpochSlice | None:
    native = [epoch for epoch in epochs if epoch.harness == NATIVE_HARNESS]
    if len(native) < 2:
        return None
    epoch = next(epoch for epoch in native if epoch.seq == seq)
    return NativeEpochSlice(
        not_before=None if epoch is native[0] else epoch.started_at,
        before=epoch.ended_at,
        first=epoch is native[0],
    )


def apply_native_slice(
    items: Sequence[TimelineItem], epoch_slice: NativeEpochSlice | None
) -> list[TimelineItem]:
    if epoch_slice is None:
        return list(items)
    stamps = effective_native_stamps([item.created_at for item in items])
    return [item for item, stamp in zip(items, stamps, strict=True) if epoch_slice.holds(stamp)]
