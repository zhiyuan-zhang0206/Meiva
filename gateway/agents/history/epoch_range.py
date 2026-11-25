# meiva:link gateway/agents/history/docs/epoch-timeline.md

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from base.agents.epochs.model import HarnessEpoch
from base.agents.epochs.native_slice import moment_of
from base.agents.history.timeline_item import TimelineItem

__all__ = ["ExternalRange", "external_range", "in_time_range"]


@dataclass(frozen=True)
class ExternalRange:
    floor: int | None
    ceiling: int | None
    since: datetime | None
    until: datetime | None

    @property
    def timed(self) -> bool:
        return self.since is not None or self.until is not None


def _ceiling(siblings: Sequence[HarnessEpoch], epoch: HarnessEpoch) -> int | None:
    if epoch.source_end is not None or epoch.ended_at is None:
        return epoch.source_end
    later = [o.source_start for o in siblings if o.seq > epoch.seq and o.source_start is not None]
    return min(later, default=None)


def _floor(siblings: Sequence[HarnessEpoch], epoch: HarnessEpoch) -> int | None:
    if epoch.source_start is not None:
        return epoch.source_start
    earlier = [o.source_end for o in siblings if o.seq < epoch.seq and o.source_end is not None]
    return max(earlier, default=None)


def external_range(epochs: Sequence[HarnessEpoch], epoch: HarnessEpoch) -> ExternalRange:
    siblings = [other for other in epochs if other.harness == epoch.harness and other is not epoch]
    floor, ceiling = _floor(siblings, epoch), _ceiling(siblings, epoch)
    if floor is None and ceiling is None and siblings:
        return ExternalRange(None, None, epoch.started_at, epoch.ended_at)
    if floor is not None and ceiling is not None:
        ceiling = max(ceiling, floor)
    return ExternalRange(floor, ceiling, None, None)


def in_time_range(item: TimelineItem, since: datetime | None, until: datetime | None) -> bool:
    if item.created_at is None:
        return True
    moment = moment_of(item.created_at)
    if moment is None:
        return True
    return (since is None or moment >= since) and (until is None or moment < until)
