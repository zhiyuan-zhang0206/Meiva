from __future__ import annotations

import json
from pathlib import Path

from base.telemetry.metrics.core import catalog

_DASHBOARD = (
    Path(__file__).resolve().parents[5]
    / "deploy/lgtm/config/grafana/provisioning/dashboards/meiva-ops-main.json"
)


def _what_happened_query() -> str:
    [spec] = [s for s in catalog.collect_core_metrics() if s.name == "core_events_what_happened"]
    return spec.query


def test_the_exec_memory_guard_kill_is_a_listed_anomaly() -> None:
    assert "|exec_memory_guard_killed|" in _what_happened_query()


def test_the_shipped_dashboard_lists_it_in_the_same_panel() -> None:
    dashboard = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    expressions = [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if "expr" in target
    ]
    assert any("|exec_memory_guard_killed|" in expr for expr in expressions)


# meiva:note
# The panel lists names by hand, so a declared anomaly that is not on it is a row nobody sees.
# `task_reminder_undelivered` reports a delivery path that stopped, and this board is the only
# place that reading is visible without writing a query (task #6399).
def test_the_undelivered_reminder_count_is_a_listed_anomaly() -> None:
    assert "|task_reminder_undelivered|" in _what_happened_query()


# meiva:note
# The shipped dashboard is the fixture the render is compared against, so the list exists twice
# and pinning the read render alone would leave the provisioned board silently missing the row.
def test_the_shipped_dashboard_lists_the_undelivered_count_in_the_same_panel() -> None:
    dashboard = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    expressions = [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if "expr" in target
    ]
    assert any("|task_reminder_undelivered|" in expr for expr in expressions)
