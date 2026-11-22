# meiva:note
# Pure unit tests for diagnostic archive verdicts and dashboard rollups.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gateway.cluster.monitor.diagnostics import archive_reason, diagnostic_totals
from gateway.cluster.schemas import (
    DiagnosticArchiveReason,
    DiagnosticClassRow,
    DiagnosticLevel,
    StatsDiagnostics,
)


def _row(
    level: DiagnosticLevel,
    count: int,
    *,
    current_sha_count: int,
    archived_by: DiagnosticArchiveReason | None,
) -> DiagnosticClassRow:
    now = datetime.now(UTC)
    return DiagnosticClassRow(
        level=level,
        event_name="e",
        source="svc",
        process="gateway",
        category="telemetry",
        count=count,
        current_sha_count=current_sha_count,
        first_seen=now,
        last_seen=now,
        archived_by=archived_by,
    )


def test_archive_reason_prioritizes_deployed_sha_then_quiet_window() -> None:
    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    quiet = timedelta(hours=1)

    assert (
        archive_reason(
            current_sha="sha-new",
            current_sha_last_seen=None,
            last_seen=now - timedelta(minutes=1),
            at=now,
            quiet_window=quiet,
        )
        == "deployed_sha"
    )
    assert (
        archive_reason(
            current_sha="sha-new",
            current_sha_last_seen=now - timedelta(hours=1),
            last_seen=now - timedelta(hours=1),
            at=now,
            quiet_window=quiet,
        )
        == "quiet"
    )
    assert (
        archive_reason(
            current_sha="sha-new",
            current_sha_last_seen=now - timedelta(minutes=59),
            last_seen=now - timedelta(minutes=59),
            at=now,
            quiet_window=quiet,
        )
        is None
    )
    assert (
        archive_reason(
            current_sha=None,
            current_sha_last_seen=None,
            last_seen=now - timedelta(minutes=10),
            at=now,
            quiet_window=quiet,
        )
        is None
    )


def test_totals_split_active_current_sha_events_from_archived_ones() -> None:
    rows = [
        _row("warning", 4, current_sha_count=4, archived_by=None),
        _row("error", 10, current_sha_count=2, archived_by=None),
        _row("critical", 5, current_sha_count=0, archived_by="deployed_sha"),
        _row("error", 3, current_sha_count=3, archived_by="quiet"),
    ]

    assert diagnostic_totals(rows, deployed_sha="sha-new") == StatsDiagnostics(
        active_classes=2,
        archived_classes=2,
        active_events=6,
        archived_events=16,
        errors=2,
        warnings=4,
        deployed_sha="sha-new",
    )


def test_totals_of_no_classes_are_zero() -> None:
    assert diagnostic_totals([], deployed_sha=None) == StatsDiagnostics()
