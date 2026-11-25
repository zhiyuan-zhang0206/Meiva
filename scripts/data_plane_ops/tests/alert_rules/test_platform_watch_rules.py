from __future__ import annotations

from typing import Any, NamedTuple

import pytest

from scripts.data_plane_ops.tests.alert_rules.rule_helpers import (
    load_rules,
    rule_query_exprs,
)


class Pin(NamedTuple):
    severity: str
    pending: str
    evaluator: str
    threshold: float
    metric: str


_PINS: dict[str, Pin] = {
    "meiva-ops-host-disk-free-low": Pin("warning", "5m", "lt", 20, "host_disk_free"),
    "meiva-ops-host-disk-free-critical": Pin("critical", "2m", "lt", 10, "host_disk_free"),
    "meiva-ops-host-disk-draining": Pin("warning", "10m", "lt", 6, "host_disk_draining"),
    "meiva-ops-host-swap-used-high": Pin("warning", "10m", "gt", 8, "host_swap"),
    "meiva-ops-host-swap-used-critical": Pin("critical", "5m", "gt", 15, "host_swap"),
    "meiva-ops-pgbouncer-write-stall": Pin("critical", "3m", "gt", 0, "pgbouncer_write_stall"),
    "meiva-ops-otelcol-receiver-refused": Pin(
        "warning", "10m", "gt", 0, "otelcol_receiver_refused"
    ),
}


def _rules() -> dict[str, dict[str, Any]]:
    return {rule["uid"]: rule for rule in load_rules()}


def _condition(rule: dict[str, Any]) -> tuple[str, float]:
    (node,) = [d["model"] for d in rule["data"] if d["refId"] == "D"]
    (condition,) = node["conditions"]
    return condition["evaluator"]["type"], condition["evaluator"]["params"][0]


@pytest.mark.parametrize("uid", sorted(_PINS))
def test_the_rule_carries_its_pinned_severity_debounce_and_threshold(uid: str) -> None:
    pin = _PINS[uid]
    rule = _rules()[uid]
    assert rule["labels"]["severity"] == pin.severity
    assert rule["labels"]["metric"] == pin.metric
    assert rule["labels"]["ruleUID"] == uid
    assert rule["for"] == pin.pending
    assert _condition(rule) == (pin.evaluator, pin.threshold)


@pytest.mark.parametrize("uid", sorted(_PINS))
def test_the_rule_groups_by_machine_and_names_it(uid: str) -> None:
    rule = _rules()[uid]
    (expr,) = rule_query_exprs(rule, "prometheus")
    assert "by (machine)" in expr
    assert "{{ $labels.machine }}" in rule["annotations"]["summary"]


def test_both_free_space_tiers_judge_the_same_number() -> None:
    low = rule_query_exprs(_rules()["meiva-ops-host-disk-free-low"], "prometheus")
    critical = rule_query_exprs(_rules()["meiva-ops-host-disk-free-critical"], "prometheus")
    assert low == critical
    assert 'state="free"' in low[0]
    assert 'mountpoint=~"/|/System/Volumes/Data"' in low[0]


def test_the_draining_rule_only_exists_above_the_hourly_loss_floor() -> None:
    (expr,) = rule_query_exprs(_rules()["meiva-ops-host-disk-draining"], "prometheus")
    assert "offset 1h" in expr
    assert "> 8 * 2^30" in expr


def test_the_stall_rule_keeps_the_sentinels_judgement() -> None:
    (expr,) = rule_query_exprs(_rules()["meiva-ops-pgbouncer-write-stall"], "prometheus")
    assert "pgbouncer_query_time_microseconds" in expr
    assert "> 300000" in expr
    assert "pgbouncer_transactions_per_second" in expr
    assert "< 20" in expr


def test_both_swap_tiers_read_the_one_machine_wide_swap_figure() -> None:
    warning = rule_query_exprs(_rules()["meiva-ops-host-swap-used-high"], "prometheus")
    critical = rule_query_exprs(_rules()["meiva-ops-host-swap-used-critical"], "prometheus")
    assert warning == critical
    assert 'max by (machine) (system_paging_usage_bytes{machine!="", state="used"})' in warning[0]


@pytest.mark.parametrize(
    "uid", ["meiva-ops-host-swap-used-high", "meiva-ops-host-swap-used-critical"]
)
def test_a_silent_swap_source_is_not_an_alert(uid: str) -> None:
    assert _rules()[uid]["noDataState"] == "OK"


def test_the_intake_refusal_rule_reads_one_counter_so_series_cannot_collide() -> None:
    (expr,) = rule_query_exprs(_rules()["meiva-ops-otelcol-receiver-refused"], "prometheus")
    assert expr.count("otelcol_receiver_refused_") == 1
    assert "increase(" in expr
