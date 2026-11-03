"""Coverage-note routing for the inspector metrics read model (task #3869).

Expected limits (historical coverage, legacy archive precision) log at DEBUG
and emit no event. An unexpected limit (``missing_turn_durations`` on a window
inside the collection era) emits the ``inspect_metrics_coverage_gap`` WARNING
event on every read that still shows it. The note is best-effort: it must
never raise into the statistics read path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from base.log.tests.log_records import LogRecords
from gateway.inspect import _metrics_health as imh
from gateway.inspect._coverage import (
    CoverageFacts,
    CoverageNote,
    coverage_notes,
    window_is_historical,
)
from gateway.inspect.schemas import InspectMetricsMetadata, MetricEvidence

T0 = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)  # collection started
T2 = datetime(2026, 9, 17, 13, 0, tzinfo=UTC)


def _ev(
    availability: str = "observed",
    reason: str | None = None,
    retained: tuple[str, ...] = (),
) -> MetricEvidence:
    return MetricEvidence(
        availability=availability,  # pyright: ignore[reportArgumentType]
        sources=["observations"],
        reason=reason,  # pyright: ignore[reportArgumentType]
        retained_unapplied_sources=list(retained),
    )


def _md(
    *,
    cost: MetricEvidence | None = None,
    turns: MetricEvidence | None = None,
    activity: MetricEvidence | None = None,
    lifecycle: MetricEvidence | None = None,
    window_start: datetime | None = T0,
) -> InspectMetricsMetadata:
    return InspectMetricsMetadata(
        window_start=window_start,
        window_end=T2,
        sampled_at=T2,
        collection_started_at=T1,
        last_observed_at=T2,
        cost=cost or _ev(),
        turns=turns or _ev(),
        activity=activity or _ev(),
        lifecycle=lifecycle or _ev(),
    )


def _facts(
    *,
    cost: MetricEvidence | None = None,
    turns: MetricEvidence | None = None,
    activity: MetricEvidence | None = None,
    lifecycle: MetricEvidence | None = None,
    window_start: datetime | None = T0,
) -> CoverageFacts:
    return imh.coverage_facts(
        _md(
            cost=cost,
            turns=turns,
            activity=activity,
            lifecycle=lifecycle,
            window_start=window_start,
        )
    )


def _gap_events_at(records: LogRecords, level: str) -> list[dict[str, object]]:
    return records.extras(event="inspect_metrics_coverage_gap", level=level)


def _gap_events(records: LogRecords) -> list[dict[str, object]]:
    return records.extras(event="inspect_metrics_coverage_gap")


def test_expected_limit_logs_at_debug_and_emits_no_event(
    loguru_records: LogRecords,
) -> None:
    md = _md(cost=_ev("partial", "historical_coverage_unknown"))
    imh.note_inspect_metrics_coverage(42, md, spawned_at=T0)  # window reaches before collection
    limits = loguru_records.extras(message="coverage limit")
    assert len(limits) == 1
    assert len(loguru_records.extras(message="coverage limit", level="DEBUG")) == 1
    assert limits[0]["condition"] == "historical_coverage_unknown"
    assert limits[0]["family"] == "cost"
    assert _gap_events(loguru_records) == []


@pytest.mark.parametrize(
    ("evidence", "condition"),
    [
        (_ev("partial", "historical_coverage_unknown"), "historical_coverage_unknown"),
        (_ev("partial", "missing_turn_durations"), "missing_turn_durations"),
        (_ev("partial", "archive_precision_unattributed"), "archive_precision_unattributed"),
        (
            _ev("partial", None, retained=("historical_archive_distribution",)),
            "archive_precision_unattributed",
        ),
        # missing durations stays primary when the retained-source caveat rides along
        (
            _ev("partial", "missing_turn_durations", retained=("historical_archive_distribution",)),
            "missing_turn_durations",
        ),
    ],
)
def test_condition_classification(evidence: MetricEvidence, condition: str) -> None:
    notes = coverage_notes(_facts(turns=evidence, window_start=T0), spawned_at=T0)
    assert [(n.family, n.condition, n.availability) for n in notes] == [
        ("turns", condition, evidence.availability)
    ]


def test_a_clean_read_has_no_notes() -> None:
    assert coverage_notes(_facts(window_start=T1), spawned_at=T0) == []


def test_missing_durations_is_unexpected_only_inside_the_collection_era() -> None:
    evidence = _ev("partial", "missing_turn_durations")
    live = coverage_notes(_facts(turns=evidence, window_start=T1), spawned_at=T0)
    historical = coverage_notes(_facts(turns=evidence, window_start=T0), spawned_at=T0)
    assert live == [CoverageNote("turns", "missing_turn_durations", "partial", unexpected=True)]
    assert historical == [
        CoverageNote("turns", "missing_turn_durations", "partial", unexpected=False)
    ]


def test_a_window_is_historical_when_it_reaches_before_the_collection_started() -> None:
    assert window_is_historical(_facts(window_start=T0), spawned_at=T0)
    # meiva:note
    # unknown reads quiet
    assert window_is_historical(_facts(window_start=None), spawned_at=T0)
    assert not window_is_historical(_facts(window_start=T1), spawned_at=T0)
    # meiva:note
    # an agent born inside the collection era never reaches back before it
    assert not window_is_historical(_facts(window_start=T0), spawned_at=T1)


def test_unexpected_missing_durations_emits_the_gap_event_on_every_read(
    loguru_records: LogRecords,
) -> None:
    # Window starts inside the collection era -> the gap is live (unexpected).
    bad = _md(turns=_ev("partial", "missing_turn_durations"), window_start=T1)
    imh.note_inspect_metrics_coverage(42, bad, spawned_at=T0)
    imh.note_inspect_metrics_coverage(42, bad, spawned_at=T0)  # still true: emitted again
    events = _gap_events(loguru_records)
    assert len(events) == 2
    assert len(_gap_events_at(loguru_records, "WARNING")) == 2
    extra = events[0]
    assert (extra["agent_id"], extra["family"], extra["condition"]) == (
        42,
        "turns",
        "missing_turn_durations",
    )
    assert extra["availability"] == "partial"


def test_missing_durations_in_a_historical_window_is_not_a_gap(
    loguru_records: LogRecords,
) -> None:
    md = _md(turns=_ev("partial", "missing_turn_durations"), window_start=T0)
    imh.note_inspect_metrics_coverage(42, md, spawned_at=T0)
    assert _gap_events(loguru_records) == []


def test_a_clean_read_emits_nothing(loguru_records: LogRecords) -> None:
    imh.note_inspect_metrics_coverage(42, _md(window_start=T1), spawned_at=T0)
    assert _gap_events(loguru_records) == []
    assert not loguru_records.extras(message="coverage limit")


def test_note_never_raises_into_the_read_path(
    loguru_records: LogRecords,
) -> None:
    # meiva:note
    # A metadata value built without its fields: reading any of them fails.
    broken = InspectMetricsMetadata.model_construct()
    imh.note_inspect_metrics_coverage(42, broken, spawned_at=T0)
    assert any("note failed" in message for message in loguru_records.messages())
