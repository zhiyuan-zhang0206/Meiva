from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

# meiva:note
# A recorded gap is a WAL range this cluster has accepted as permanently missing.
# The record lives in wal-gaps.json beside this module, so adding one is a
# reviewed change rather than a verifier setting: the chain check keeps failing
# for every gap the file does not name.
# meiva:link services/backup/walg/docs/walg-tick.meiva.okf.md
RECORDED_GAPS_PATH = Path(__file__).with_name("wal-gaps.json")

_SEGMENT = re.compile(r"^[0-9A-F]{24}$")


# meiva:note
# A malformed record stops the chain check: guessing which gaps are accepted
# would turn a data defect into a green verify.
# meiva:link services/backup/walg/docs/walg-tick.meiva.okf.md
class RecordedGapError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecordedGap:
    start_segment: str
    end_segment: str
    segments: int
    reason: str
    occurred_on: str

    def covers(self, start_segment: str, end_segment: str) -> bool:
        return self.start_segment <= start_segment and end_segment <= self.end_segment


@dataclass(frozen=True)
class MissingRange:
    start_segment: str
    end_segment: str
    segments: int


def load_recorded_gaps(path: Path | None = None) -> tuple[RecordedGap, ...]:
    source = path or RECORDED_GAPS_PATH
    try:
        payload: Any = json.loads(source.read_text(encoding="utf-8"))
        entries = cast("dict[str, Any]", payload)["gaps"]
        return tuple(_gap(cast("dict[str, Any]", entry)) for entry in entries)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RecordedGapError(f"{source}: recorded WAL gaps are unreadable ({exc})") from None


def _gap(entry: dict[str, Any]) -> RecordedGap:
    gap = RecordedGap(
        start_segment=str(entry["start_segment"]),
        end_segment=str(entry["end_segment"]),
        segments=int(entry["segments"]),
        reason=str(entry["reason"]),
        occurred_on=str(entry["occurred_on"]),
    )
    for segment in (gap.start_segment, gap.end_segment):
        if not _SEGMENT.match(segment):
            raise RecordedGapError(f"not a WAL segment name: {segment!r}")
    if gap.end_segment < gap.start_segment:
        raise RecordedGapError(f"gap ends before it starts: {gap.start_segment}..{gap.end_segment}")
    if gap.segments < 1:
        raise RecordedGapError(f"gap claims {gap.segments} segments: {gap.start_segment}")
    return gap


def covering_gap(missing: MissingRange, recorded: Sequence[RecordedGap]) -> RecordedGap | None:
    for gap in recorded:
        if gap.covers(missing.start_segment, missing.end_segment):
            return gap
    return None


def partition_by_record(
    missing: Iterable[MissingRange], recorded: Sequence[RecordedGap]
) -> tuple[tuple[MissingRange, ...], tuple[RecordedGap, ...]]:
    unrecorded: list[MissingRange] = []
    accepted: list[RecordedGap] = []
    for item in missing:
        gap = covering_gap(item, recorded)
        if gap is None:
            unrecorded.append(item)
        elif gap not in accepted:
            accepted.append(gap)
    return tuple(unrecorded), tuple(accepted)
