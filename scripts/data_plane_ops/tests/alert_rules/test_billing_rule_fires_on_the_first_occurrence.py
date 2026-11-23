from __future__ import annotations

import json

import pytest
import yaml

from scripts.data_plane_ops.tests.alert_rules.rule_helpers import (
    INFRA_RULE_METRICS,
    RULES,
    assert_latency_query_scopes_to_route_class,
    assert_reduce_keeps_route_label,
    load_rules,
    rule_query_exprs,
    threshold_params,
)


def test_billing_rule_fires_on_the_first_occurrence() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-llm-billing-quota"]
    assert threshold_params(rule) == [[0]]
    assert rule["for"] == "0m"
    assert rule["labels"]["severity"] == "critical"
    assert any("[15m]" in e for e in rule_query_exprs(rule, "loki"))


def test_rate_limit_rule_groups_http_429s_by_provider() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-llm-rate-limit"]
    expr = rule_query_exprs(rule, "loki")[0]

    assert "sum by (attributes_vendor)" in expr
    assert 'attributes_status="429"' in expr
    assert "[5m]" in expr
    assert threshold_params(rule) == [[5]]
    assert rule["for"] == "0m"
    assert rule["labels"]["severity"] == "warning"
    assert "{{ $labels.attributes_vendor }}" in rule["annotations"]["summary"]


def test_billing_rule_keys_on_the_billing_flag_not_a_status_list() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    expr = rule_query_exprs(rules["meiva-ops-llm-billing-quota"], "loki")[0]
    assert 'attributes_billing="true"' in expr
    assert "402" not in expr, "the rule must not enumerate provider statuses itself"


def test_billing_rule_names_vendor_and_model_in_the_notification() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-llm-billing-quota"]
    assert "sum by (attributes_vendor, attributes_model)" in rule_query_exprs(rule, "loki")[0]
    summary = rule["annotations"]["summary"]
    assert "{{ $labels.attributes_vendor }}" in summary
    assert "{{ $labels.attributes_model }}" in summary
    assert len(summary) <= 200, f"summary is {len(summary)} chars, IM truncates at 200"


def test_delivery_stalled_rule_filters_fresh_by_age() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    exprs = rule_query_exprs(rules["meiva-ops-delivery-stalled-backlog"], "loki")
    assert any("attributes_age_s < 600" in e for e in exprs)


@pytest.mark.parametrize(("uid", "metric"), sorted(INFRA_RULE_METRICS.items()))
def test_infra_rule_queries_its_scraped_metric(uid: str, metric: str) -> None:
    rules = {r["uid"]: r for r in load_rules()}
    exprs = rule_query_exprs(rules[uid], "prometheus")
    assert len(exprs) == 1, f"{uid}: expected exactly one Prometheus query"
    assert metric in exprs[0], f"{uid}: does not read {metric}:\n{exprs[0]}"
    assert not rule_query_exprs(rules[uid], "loki"), f"{uid}: infra rules never query Loki"


@pytest.mark.parametrize("uid", sorted(INFRA_RULE_METRICS))
def test_infra_rule_groups_by_machine(uid: str) -> None:
    rules = {r["uid"]: r for r in load_rules()}
    expr = rule_query_exprs(rules[uid], "prometheus")[0]
    assert "by (machine)" in expr or "by (machine," in expr
    assert 'machine!=""' in expr, f"{uid}: missing the current-label branch"
    assert 'machine="", machine_name!=""' in expr, f"{uid}: missing the legacy-label fallback"
    assert "or on (machine" in expr, f"{uid}: overlapping generations can both count"
    assert "{{ $labels.machine }}" in rules[uid]["annotations"]["summary"]


@pytest.mark.parametrize(
    "uid",
    [
        "meiva-ops-host-disk-watermark",
        "meiva-ops-host-disk-watermark-93",
        "meiva-ops-host-disk-watermark-95",
    ],
)
def test_disk_watermark_rule_is_per_mountpoint(uid: str) -> None:
    rules = {r["uid"]: r for r in load_rules()}
    expr = rule_query_exprs(rules[uid], "prometheus")[0]
    assert "by (machine, mountpoint)" in expr
    assert "{{ $labels.mountpoint }}" in rules[uid]["annotations"]["summary"]


@pytest.mark.parametrize(
    "uid",
    [
        "meiva-ops-host-disk-watermark",
        "meiva-ops-host-disk-watermark-93",
        "meiva-ops-host-disk-watermark-95",
    ],
)
def test_disk_watermark_rule_excludes_non_asset_mounts(uid: str) -> None:
    rules = {r["uid"]: r for r in load_rules()}
    expr = rule_query_exprs(rules[uid], "prometheus")[0]
    assert 'mountpoint!~"/Volumes/.*|/private/tmp/.*|/private/var/folders/.*"' in expr
    assert 'mode!="ro"' in expr
    assert 'type!~"iso9660|udf|cd9660"' in expr
    assert "WindowsRunner" not in expr
    assert "by (machine, mountpoint)" in expr


def test_infra_ratio_rules_round_for_readability() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    for uid in (
        "meiva-ops-host-cpu-saturated",
        "meiva-ops-host-memory-pressure",
        "meiva-ops-host-disk-watermark",
        "meiva-ops-host-disk-watermark-93",
        "meiva-ops-host-disk-watermark-95",
        "meiva-ops-pg-connection-saturation",
    ):
        expr = rule_query_exprs(rules[uid], "prometheus")[0]
        assert "round(" in expr and "0.001)" in expr, f"{uid} is unrounded:\n{expr}"
        threshold: float = threshold_params(rules[uid])[0][0]
        assert threshold == round(threshold, 2), f"{uid}: threshold finer than the rounding"


def test_infra_rule_thresholds() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    assert threshold_params(rules["meiva-ops-host-cpu-saturated"]) == [[0.9]]
    assert threshold_params(rules["meiva-ops-host-memory-pressure"]) == [[0.9]]
    assert threshold_params(rules["meiva-ops-host-disk-watermark"]) == [[0.9]]
    assert threshold_params(rules["meiva-ops-host-disk-watermark-93"]) == [[0.93]]
    assert threshold_params(rules["meiva-ops-host-disk-watermark-95"]) == [[0.95]]
    assert threshold_params(rules["meiva-ops-pg-connection-saturation"]) == [[0.8]]
    assert threshold_params(rules["meiva-ops-redis-memory"]) == [[2147483648]]


def test_disk_watermark_escalation_tiers() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    baseline_expr = rule_query_exprs(rules["meiva-ops-host-disk-watermark"], "prometheus")
    expectations = {
        "meiva-ops-host-disk-watermark-93": ("warning", "15m"),
        "meiva-ops-host-disk-watermark-95": ("critical", "5m"),
    }

    for uid, (severity, hold) in expectations.items():
        rule = rules[uid]
        assert rule_query_exprs(rule, "prometheus") == baseline_expr
        assert rule["for"] == hold
        assert rule["labels"] == {
            "severity": severity,
            "ruleUID": uid,
            "metric": "host_disk",
            "team": "meiva-ops",
        }
        assert "{{ $labels.machine }}" in rule["annotations"]["summary"]
        assert "{{ $labels.mountpoint }}" in rule["annotations"]["summary"]
        assert "{{ $values.C }}" in rule["annotations"]["summary"]


def test_collector_queue_pressure_is_current_and_per_exporter() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-otelcol-queue-pressure"]
    expr = rule_query_exprs(rule, "prometheus")[0]
    assert "otelcol_exporter_queue_size" in expr
    assert "otelcol_exporter_queue_capacity" in expr
    assert "by (machine, exporter, data_type)" in expr
    assert "increase(" not in expr
    assert threshold_params(rule) == [[0.8]]
    assert rule["for"] == "5m"


def test_collector_enqueue_failure_rule_uses_window_delta() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-otelcol-enqueue-failures"]
    expr = rule_query_exprs(rule, "prometheus")[0]
    assert "otelcol_exporter_enqueue_failed_(log_records|metric_points|spans)_total" in expr
    assert "increase(" in expr
    assert "[5m]" in expr
    assert "sum by (machine, exporter)" in expr
    assert threshold_params(rule) == [[0]]


def test_collector_silence_rule_tracks_recently_seen_machines() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-otelcol-host-silent"]
    expr = rule_query_exprs(rule, "prometheus")[0]
    assert "otelcol_process_uptime_total" in expr
    assert "max_over_time" in expr
    assert "[24h]" in expr
    assert "[5m]" in expr
    assert "unless on(machine)" in expr
    assert "max by (machine)" in expr
    assert expr.count('machine="", machine_name!=""') == 2
    assert expr.count("or on (machine)") == 2
    assert rule["for"] == "0m", "the 5m absence window is already the debounce"
    assert "{{ $labels.machine }}" in rule["annotations"]["summary"]


def test_only_the_evaluation_meta_alert_reports_a_datasource_error() -> None:
    rules = load_rules()
    assert all(rule["noDataState"] == "OK" for rule in rules)
    non_silent = {
        rule["uid"]: rule["execErrState"] for rule in rules if rule["execErrState"] != "OK"
    }
    assert non_silent == {"meiva-ops-grafana-rule-evaluation-failed": "Error"}


@pytest.mark.parametrize(
    ("uid", "route_class", "threshold", "metric"),
    [
        ("meiva-ops-gateway-latency-route-warning", "fast", 3000, "gateway_latency_route_p95"),
        ("meiva-ops-gateway-latency-route-error", "fast", 10000, "gateway_latency_route_p95"),
        (
            "meiva-ops-gw-latency-slow-warning",
            "slow",
            5000,
            "gateway_latency_route_slow_p95",
        ),
        (
            "meiva-ops-gw-latency-slow-error",
            "slow",
            10000,
            "gateway_latency_route_slow_p95",
        ),
    ],
)
def test_gateway_latency_rules_scope_to_route_class(
    uid: str, route_class: str, threshold: int, metric: str
) -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules[uid]
    assert_latency_query_scopes_to_route_class(rule_query_exprs(rule, "loki")[0], route_class)
    assert rule["for"] == "5m"
    assert rule["labels"]["notify_im"] == "false"
    assert rule["labels"]["metric"] == metric
    assert threshold_params(rule) == [[threshold]]
    assert_reduce_keeps_route_label(rule)


def test_turn_duration_rule_depends_only_on_prometheus() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-turn-duration-p95"]
    exprs = rule_query_exprs(rule, "prometheus")
    (fleet_p95,) = exprs
    assert "meiva_turn_end_duration_seconds_bucket" in fleet_p95
    assert "histogram_quantile(0.95" in fleet_p95
    assert {node["datasourceUid"] for node in rule["data"]} == {"prometheus", "__expr__"}
    assert {node["refId"] for node in rule["data"]} == {"A", "C", "D"}
    assert "$values.N" not in rule["annotations"]["summary"]
    assert threshold_params(rule) == [[75]]
    assert rule["for"] == "10m"
    assert rule["labels"]["notify_im"] == "false"
    assert rule["labels"]["severity"] == "warning"


def test_memory_search_rows_rules() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    expectations: dict[str, tuple[str, int]] = {
        "meiva-ops-memory-search-rows-warning": ("warning", 30000),
        "meiva-ops-memory-search-rows-critical": ("critical", 100000),
    }
    for uid, (severity, threshold) in expectations.items():
        rule = rules[uid]
        exprs = rule_query_exprs(rule, "prometheus")
        assert exprs == [f"max(meiva_memory_search_stats_rows_ratio) > {threshold}"]
        assert rule_query_exprs(rule, "loki") == []
        assert rule["for"] == "2h"
        assert rule["noDataState"] == "OK"
        assert rule["execErrState"] == "OK"
        assert threshold_params(rule) == [[threshold]]
        assert rule["labels"] == {
            "severity": severity,
            "ruleUID": uid,
            "metric": "memory_search_rows",
            "team": "meiva-ops",
        }
        description = rule["annotations"]["description"]
        assert "100k" in description
        if uid == "meiva-ops-memory-search-rows-warning":
            assert "50k" in description


def test_recovery_drill_failure_rule_is_immediate_and_names_the_drill() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-recovery-drill-failed"]

    assert rule_query_exprs(rule, "loki") == []
    assert rule_query_exprs(rule, "prometheus") == [
        "min by (drill) (meiva_recovery_drill_status_last_run_ok_ratio)"
    ]
    assert rule["for"] == "0m"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"
    assert threshold_params(rule) == [[1]]
    threshold = next(d for d in rule["data"] if d["model"].get("type") == "threshold")
    assert threshold["model"]["conditions"][0]["evaluator"]["type"] == "lt"
    assert rule["labels"] == {
        "severity": "error",
        "ruleUID": "meiva-ops-recovery-drill-failed",
        "metric": "recovery_drill_failure",
        "team": "meiva-ops",
    }
    assert "{{ $labels.drill }}" in rule["annotations"]["summary"]


def test_backup_dump_failure_rule_reads_last_run_ok_gauge() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-backup-dump-failed"]

    assert rule_query_exprs(rule, "loki") == []
    assert rule_query_exprs(rule, "prometheus") == [
        "min(meiva_backup_dump_status_last_run_ok_ratio)"
    ]
    assert rule["for"] == "0m"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"
    assert threshold_params(rule) == [[1]]
    threshold = next(d for d in rule["data"] if d["model"].get("type") == "threshold")
    assert threshold["model"]["conditions"][0]["evaluator"]["type"] == "lt"
    assert rule["labels"] == {
        "severity": "error",
        "ruleUID": "meiva-ops-backup-dump-failed",
        "metric": "backup_dump_failure",
        "team": "meiva-ops",
    }


def test_tempo_backend_down_rule_tracks_the_remote_scrape() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-tempo-backend-down"]

    assert rule_query_exprs(rule, "prometheus") == ['up{job="tempo"}']
    assert rule_query_exprs(rule, "loki") == []
    assert rule["for"] == "1h"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"
    assert threshold_params(rule) == [[1]]
    assert [
        d["model"]["conditions"][0]["evaluator"]["type"]
        for d in rule["data"]
        if d["model"].get("type") == "threshold"
    ] == ["lt"]
    assert rule["labels"] == {
        "severity": "warning",
        "ruleUID": "meiva-ops-tempo-backend-down",
        "metric": "tempo_up",
        "team": "meiva-ops",
    }


def test_telemetry_queue_loss_is_an_immediate_error_on_independent_metrics() -> None:
    rule = next(r for r in load_rules() if r["uid"] == "meiva-ops-telemetry-queue-loss")
    assert rule["labels"]["severity"] == "error"
    assert rule["for"] == "0s"
    query = next(q for q in rule["data"] if q["refId"] == "A")
    assert query["datasourceUid"] == "prometheus"
    assert "meiva_event_log_drop_last_dropped_at_ratio" in query["model"]["expr"]
    assert "cluster=" not in query["model"]["expr"]
    threshold = next(q for q in rule["data"] if q["refId"] == "D")
    assert threshold["model"]["conditions"][0]["evaluator"] == {"type": "lt", "params": [300]}


def test_retired_rule_inventory_is_disjoint_and_outside_provisioning() -> None:
    inventory = RULES.parents[2] / "retired-alert-rules.json"
    uids = json.loads(inventory.read_text(encoding="utf-8"))
    assert uids and len(uids) == len(set(uids))
    live = {r["uid"] for r in load_rules()}
    assert all(uid and len(uid) <= 40 and uid not in live for uid in uids)
    assert {
        "meiva-ops-pitr-storage-growth",
        "meiva-ops-agent-restart-spike",
        "meiva-ops-root-diagnostic-task-telemetry",
        "meiva-ops-impersonation-capture-failed",
    } <= set(uids)
    assert "meiva-ops-plugin-collector-silent" not in uids
    for path in RULES.parent.iterdir():
        if path.suffix in {".yaml", ".yml", ".json"}:
            assert "deleteRules" not in yaml.safe_load(path.read_text(encoding="utf-8"))
