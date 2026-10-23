# meiva:note
# Which coverage limits one inspector statistics read shows, and whether any is unexpected.
#
# A pure reading of the read model's metadata: the shell (`_metrics_health`) logs what is returned
# here, one line per limit per read.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

__all__ = ["CoverageNote", "Evidence", "Metadata", "coverage_notes", "window_is_historical"]

_FAMILIES = ("cost", "turns", "activity", "lifecycle")


# meiva:note
# The part of a family's evidence the verdicts read.
class Evidence(Protocol):
    @property
    def availability(self) -> str: ...

    @property
    def reason(self) -> str | None: ...

    @property
    def retained_unapplied_sources(self) -> list[str]: ...


# meiva:note
# The part of the read model's metadata the verdicts read.
class Metadata(Protocol):
    @property
    def window_start(self) -> datetime | None: ...

    @property
    def collection_started_at(self) -> datetime: ...

    @property
    def cost(self) -> Evidence: ...

    @property
    def turns(self) -> Evidence: ...

    @property
    def activity(self) -> Evidence: ...

    @property
    def lifecycle(self) -> Evidence: ...


# meiva:note
# One limit of one family. `unexpected` is true only for a live recorder gap.
@dataclass(frozen=True)
class CoverageNote:
    family: str
    condition: str
    availability: str
    unexpected: bool


# meiva:note
# Mirror of the read model's ``historical`` flag (window reaches before
# the observation collection started). A missing window start reads
# historical — the quiet side.
def window_is_historical(metadata: Metadata, *, spawned_at: datetime) -> bool:
    if metadata.window_start is None:
        return True
    return max(metadata.window_start, spawned_at) < metadata.collection_started_at


def _condition(evidence: Evidence) -> str:
    if evidence.reason == "missing_turn_durations":
        return "missing_turn_durations"
    if evidence.reason == "archive_precision_unattributed" or evidence.retained_unapplied_sources:
        return "archive_precision_unattributed"
    return "historical_coverage_unknown"


# meiva:note
# Every coverage limit of one read, in family order.
#
# Limits expected by construction (a window reaching back before the observation collection
# started, a legacy archive whose per-day precision cannot be attributed per turn, retained
# sources) are chronic noise. An UNEXPECTED limit is ``missing_turn_durations`` on a window
# inside the collection era: turns recorded without their durations is a live recorder gap,
# not history.
def coverage_notes(metadata: Metadata, *, spawned_at: datetime) -> list[CoverageNote]:
    historical = window_is_historical(metadata, spawned_at=spawned_at)
    notes: list[CoverageNote] = []
    for family in _FAMILIES:
        evidence: Evidence = getattr(metadata, family)
        if evidence.availability == "observed" and not evidence.retained_unapplied_sources:
            continue
        condition = _condition(evidence)
        notes.append(
            CoverageNote(
                family=family,
                condition=condition,
                availability=evidence.availability,
                unexpected=condition == "missing_turn_durations" and not historical,
            )
        )
    return notes
