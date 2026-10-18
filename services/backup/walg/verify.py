# meiva:note
# `wal-verify integrity timeline --json` decides whether the archived WAL chain is
# whole. Only the JSON status is read: v3.0.9 exits 0 while it reports a gap.
# FAILURE means a segment between the oldest backup and the current one is
# MISSING_LOST, or storage and cluster disagree on timelines; WARNING
# (MISSING_UPLOADING, MISSING_DELAYED) is archiving still in flight, which the
# archiver probe already watches. A MISSING_LOST range named in wal-gaps.json is
# a recorded decision, not a failure; anything else stays FAILURE.
# meiva:link services/backup/walg/docs/walg-tick.meiva.okf.md
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from services.backup.walg.backups import QUERY_TIMEOUT_S
from services.backup.walg.gaps import (
    MissingRange,
    RecordedGap,
    load_recorded_gaps,
    partition_by_record,
)
from services.backup.walg.runner import run_walg

_STATUSES = frozenset({"OK", "WARNING", "FAILURE"})


class VerifyOutputError(RuntimeError):
    """`wal-verify --json` printed something this code does not understand."""


@dataclass(frozen=True)
class ChainVerdict:
    integrity: str
    timeline: str
    raw_integrity: str = ""
    recorded: tuple[RecordedGap, ...] = ()
    unrecorded: tuple[MissingRange, ...] = field(default=())

    @property
    def failed(self) -> bool:
        return "FAILURE" in (self.integrity, self.timeline)

    def summary(self) -> str:
        detail = ""
        if self.raw_integrity and self.raw_integrity != self.integrity:
            detail = f" (wal-verify said {self.raw_integrity}"
            if self.recorded:
                detail += "; recorded gaps: " + ", ".join(
                    f"{gap.start_segment}->{gap.end_segment}, {gap.segments} segments, "
                    f"{gap.occurred_on}"
                    for gap in self.recorded
                )
            detail += ")"
        if self.unrecorded and not detail:
            detail = (
                " (unrecorded: "
                + ", ".join(
                    f"{item.start_segment}->{item.end_segment}, {item.segments} segments"
                    for item in self.unrecorded
                )
                + ")"
            )
        return f"integrity {self.integrity}{detail}, timeline {self.timeline}"


# meiva:note
# The two check statuses in the report, with recorded gaps subtracted from the
# integrity verdict: a MISSING_LOST range named in wal-gaps.json leaves the
# effective status OK, an unnamed one keeps it FAILURE, and an unknown status is
# an error rather than a pass.
# meiva:link services/backup/walg/docs/walg-tick.meiva.okf.md
def parse_verdict(text: str, *, recorded_gaps: Sequence[RecordedGap] = ()) -> ChainVerdict:
    try:
        payload: Any = json.loads(text)
        report = cast("dict[str, Any]", payload)
        integrity = cast("dict[str, Any]", report["integrity"])
        statuses = (
            str(integrity["status"]),
            str(cast("dict[str, Any]", report["timeline"])["status"]),
        )
    except (ValueError, KeyError, TypeError):
        raise VerifyOutputError(
            "wal-verify did not print the integrity and timeline checks"
        ) from None
    unknown = [status for status in statuses if status not in _STATUSES]
    if unknown:
        raise VerifyOutputError(f"wal-verify reported an unknown status: {unknown[0]!r}")
    raw_integrity = statuses[0]
    unrecorded, recorded = partition_by_record(_missing_lost(integrity), recorded_gaps)
    effective = (
        "OK" if raw_integrity == "FAILURE" and recorded and not unrecorded else raw_integrity
    )
    return ChainVerdict(
        integrity=effective,
        timeline=statuses[1],
        raw_integrity=raw_integrity,
        recorded=recorded,
        unrecorded=unrecorded,
    )


def _missing_lost(integrity: dict[str, Any]) -> tuple[MissingRange, ...]:
    details = integrity.get("details")
    if not isinstance(details, list):
        return ()
    missing: list[MissingRange] = []
    for item in cast("list[Any]", details):
        detail = cast("dict[str, Any]", item) if isinstance(item, dict) else {}
        if detail.get("status") != "MISSING_LOST":
            continue
        try:
            missing.append(
                MissingRange(
                    start_segment=str(detail["start_segment"]),
                    end_segment=str(detail["end_segment"]),
                    segments=int(detail["segments_count"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(missing)


# meiva:note
# Run the check against the bucket; it lists every archived segment and asks
# Postgres for its current segment and timeline, so it is handed the owner-only
# admin socket rather than libpq's default.
# meiva:link services/backup/walg/docs/walg-tick.meiva.okf.md
def verify_chain(pg_admin_url: str, *, path_reader: Callable[[], Path | None]) -> ChainVerdict:
    text = run_walg(
        ["wal-verify", "integrity", "timeline", "--json"],
        timeout_s=QUERY_TIMEOUT_S,
        pg_admin_url=pg_admin_url,
        path_reader=path_reader,
    )
    return parse_verdict(text, recorded_gaps=load_recorded_gaps())
