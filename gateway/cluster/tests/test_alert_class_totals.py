# meiva:note
# The dashboard's class counts: pure over the rows the alert-class read returns.
# meiva:link gateway/cluster/docs/alert-classes-api.meiva.okf.md

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from gateway.cluster.monitor.alert_classes import AlertClassTotals, alert_class_totals
from gateway.cluster.schemas import AlertClassRow, AlertLevel


def _class_row(
    level: AlertLevel, count: int, resolved_by: Literal["dismissed", "quiet"] | None
) -> AlertClassRow:
    now = datetime.now(UTC)
    return AlertClassRow(
        level=level,
        event_name="e",
        source="svc",
        process="gateway",
        category="telemetry",
        count=count,
        first_seen=now,
        last_seen=now,
        dismissal_id=None,
        resolved_by=resolved_by,
    )


# meiva:note
# Critical folds into errors, and each side's event count comes from `resolved_by` alone, so a
# class that stopped happening leaves the warnings/errors totals while staying in the history.
def test_totals_split_event_counts_by_level_and_resolution() -> None:
    rows = [
        _class_row("warning", 4, None),
        _class_row("warning", 3, "quiet"),
        _class_row("error", 2, None),
        _class_row("critical", 5, "dismissed"),
    ]

    assert alert_class_totals(rows) == AlertClassTotals(
        warnings=4,
        errors=2,
        warnings_resolved=3,
        errors_resolved=5,
        classes_unresolved=2,
        classes_resolved=2,
    )


def test_totals_of_no_classes_are_zero() -> None:
    assert alert_class_totals([]) == AlertClassTotals(
        warnings=0,
        errors=0,
        warnings_resolved=0,
        errors_resolved=0,
        classes_unresolved=0,
        classes_resolved=0,
    )
