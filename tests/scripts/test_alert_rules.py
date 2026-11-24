from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from base.events.contract import payload_keys
from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION
from base.telemetry.otlp.telemetry_otlp_metrics import strip_unit_suffix, unit_for
from scripts.data_plane_ops.tests.alert_rules.rule_helpers import (
    INFRA_RULE_METRICS as INFRA_RULE_METRICS,
)
from scripts.data_plane_ops.tests.alert_rules.rule_helpers import (
    RULES as RULES,
)
from scripts.data_plane_ops.tests.alert_rules.rule_helpers import (
    assert_latency_query_scopes_to_route_class as assert_latency_query_scopes_to_route_class,
)
from scripts.data_plane_ops.tests.alert_rules.rule_helpers import (
    assert_reduce_keeps_route_label as assert_reduce_keeps_route_label,
)
from scripts.data_plane_ops.tests.alert_rules.rule_helpers import (
    load_groups as _load_groups,
)
from scripts.data_plane_ops.tests.alert_rules.rule_helpers import (
    load_rules as load_rules,
)
from scripts.data_plane_ops.tests.alert_rules.rule_helpers import (
    rule_query_exprs as rule_query_exprs,
)
from scripts.data_plane_ops.tests.alert_rules.rule_helpers import (
    threshold_params as threshold_params,
)
from tests.scripts.test_alert_rules_signals import SIGNALS

_EXPECTED_UIDS = {
    "meiva-ops-alert-pipeline-watchdog",
    "meiva-ops-alert-pipeline-silent",
    "meiva-ops-grafana-rule-evaluation-failed",
    "meiva-ops-warning-error-spike",
    "meiva-ops-llm-rate-limit",
    "meiva-ops-sse-drop-backlog",
    "meiva-ops-telemetry-queue-loss",
    "meiva-ops-llm-latency-p95",
    "meiva-ops-delivery-stalled-backlog",
    "meiva-ops-events-freshness",
    "meiva-ops-events-low-water",
    "meiva-ops-gateway-metrics-silent",
    "meiva-ops-main-red-watch-silent",
    "meiva-ops-root-health-stale",
    "meiva-ops-checkpoint-blobs-freshness",
    "meiva-ops-checkpoint-blobs-growth",
    "meiva-ops-inbound-consumed-unknown",
    "meiva-ops-llm-billing-quota",
    "meiva-ops-gateway-latency-route-warning",
    "meiva-ops-gateway-latency-route-error",
    "meiva-ops-turn-duration-p95",
    "meiva-ops-gw-latency-slow-warning",
    "meiva-ops-gw-latency-slow-error",
    "meiva-ops-host-cpu-saturated",
    "meiva-ops-host-memory-pressure",
    "meiva-ops-host-disk-watermark",
    "meiva-ops-host-disk-watermark-93",
    "meiva-ops-host-disk-watermark-95",
    "meiva-ops-pg-connection-saturation",
    "meiva-ops-redis-memory",
    "meiva-ops-otelcol-queue-pressure",
    "meiva-ops-otelcol-enqueue-failures",
    "meiva-ops-otelcol-host-silent",
    "meiva-ops-prom-too-old-samples",
    "meiva-ops-memory-search-rows-warning",
    "meiva-ops-memory-search-rows-critical",
    "meiva-ops-recovery-drill-failed",
    "meiva-ops-backup-dump-failed",
    "meiva-ops-tempo-backend-down",
    "meiva-ops-schedule-process-lost",
    "meiva-ops-schedule-stalled",
    "meiva-ops-schedule-convergence-abandoned",
}


def test_rules_have_expected_uids() -> None:
    rules = load_rules()
    assert {r["uid"] for r in rules} == _EXPECTED_UIDS | set(SIGNALS)


def test_rule_uids_fit_grafana_40_char_limit() -> None:
    for rule in load_rules():
        assert len(rule["uid"]) <= 40, (
            f"uid {rule['uid']!r} is {len(rule['uid'])} chars — Grafana caps "
            "alert-rule UIDs at 40 and fails the whole provisioning file"
        )


def test_low_cost_rules_use_the_slow_group() -> None:
    groups = _load_groups()
    group_for_rule = {rule["uid"]: group["name"] for group in groups for rule in group["rules"]}
    for uid in (
        "meiva-ops-llm-rate-limit",
        "meiva-ops-llm-billing-quota",
        "meiva-ops-gateway-latency-route-warning",
        "meiva-ops-gateway-latency-route-error",
        "meiva-ops-turn-duration-p95",
        "meiva-ops-gw-latency-slow-warning",
        "meiva-ops-gw-latency-slow-error",
        "meiva-ops-tempo-backend-down",
    ):
        assert group_for_rule[uid] == "meiva-ops-slow"


def test_rules_never_query_postgres() -> None:
    for rule in load_rules():
        for d in rule["data"]:
            if d.get("datasourceUid") == "__expr__":
                continue
            assert d["datasourceUid"] in {"loki", "prometheus"}, (
                f"{rule['uid']}: still queries {d['datasourceUid']!r}"
            )
            model = d["model"]
            assert model["datasource"]["type"] == d["datasourceUid"], (
                f"{rule['uid']}: datasource type/uid mismatch"
            )


def test_severity_is_critical_warning_error() -> None:
    for rule in load_rules():
        severity = rule["labels"].get("severity")
        assert severity in {"critical", "warning", "error"}, (
            f"{rule['uid']}: severity {severity!r} not in the new vocabulary"
        )


def test_loki_rules_match_event_labels_in_the_selector() -> None:
    for rule in load_rules():
        for expr in rule_query_exprs(rule, "loki"):
            assert "| json" in expr, f"{rule['uid']}: no | json stage:\n{expr}"
            # meiva:note
            # This used to require `service_name="unknown_service"`, a stand-in for "emitted by a
            # process with no bound telemetry pipeline". #6352 binds that pipeline at the schedule
            # entry, so the value becomes a real process name and a rule pinned to `unknown_service`
            # would go silent exactly when its event starts arriving (#6400). The selector itself is
            # checked below — braces present, and `event_name` as a matcher rather than a `| json`
            # filter; whether a rule also pins a service stays its own choice.
            selector = expr.split("| json")[0]
            assert "{" in selector and "}" in selector
            pipeline = expr.split("| json")[1]
            assert "event_name" not in pipeline, (
                f"{rule['uid']}: event_name filter after | json (must be a "
                f"stream-selector matcher):\n{expr}"
            )


def test_loki_rules_filter_to_prod_cluster_after_json() -> None:
    for rule in load_rules():
        for expr in rule_query_exprs(rule, "loki"):
            normalized = " ".join(expr.split())
            assert '| json | cluster=".meiva"' in normalized, (
                f"{rule['uid']}: missing prod cluster filter after json:\n{expr}"
            )


@pytest.mark.parametrize(
    ("uid", "event_filter"),
    [
        ("meiva-ops-sse-drop-backlog", 'event_name=~"sse_drop|event_log_drop"'),
        ("meiva-ops-delivery-stalled-backlog", 'event_name="delivery_stalled"'),
        ("meiva-ops-llm-rate-limit", 'event_name="llm_provider_error"'),
        ("meiva-ops-llm-billing-quota", 'event_name="llm_provider_error"'),
    ],
)
def test_event_name_rules_filter_by_event_name(uid: str, event_filter: str) -> None:
    rules = {r["uid"]: r for r in load_rules()}
    exprs = rule_query_exprs(rules[uid], "loki")
    assert any(event_filter in e for e in exprs)
    assert any(event_filter in e.split("| json")[0] for e in exprs), (
        f"{uid}: event_name matcher must be a stream selector, got:\n{exprs}"
    )
    assert any('category="telemetry"' in e for e in exprs)
    assert any('category="telemetry"' in e.split("| json")[1] for e in exprs)


def test_drop_backlog_rule_unwraps_payload_n() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    exprs = rule_query_exprs(rules["meiva-ops-sse-drop-backlog"], "loki")
    assert any("unwrap attributes_n" in e for e in exprs), (
        f"R2 must unwrap attributes_n, got:\n{exprs[0]}"
    )
    assert any("sum_over_time" in e for e in exprs)


def test_level_rule_scopes_to_observability_categories() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    exprs = rule_query_exprs(rules["meiva-ops-warning-error-spike"], "loki")
    assert any('category=~"telemetry|log"' in e for e in exprs)
    assert any('level=~"warning|error|critical"' in e for e in exprs)
    assert any("[5m]" in e for e in exprs)
    assert any("[15m] offset 5m" in e for e in exprs)


def test_freshness_rule_is_absent_over_time() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    exprs = rule_query_exprs(rules["meiva-ops-events-freshness"], "loki")
    assert any("absent_over_time" in e for e in exprs)
    assert any("[5m]" in e for e in exprs)
    assert threshold_params(rules["meiva-ops-events-freshness"]) == [[0]]


def test_events_low_water_rule_detects_partial_write_loss() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-events-low-water"]

    assert rule_query_exprs(rule, "loki") == [
        'sum(count_over_time({service_name="unknown_service"} | json | '
        'cluster=".meiva" [5m])) or vector(0)'
    ]
    assert rule["for"] == "10m"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"
    assert rule["labels"] == {
        "severity": "warning",
        "ruleUID": "meiva-ops-events-low-water",
        "metric": "events_freshness",
        "team": "meiva-ops",
    }
    assert threshold_params(rule) == [[150]]
    threshold = next(d for d in rule["data"] if d["model"].get("type") == "threshold")
    assert threshold["model"]["conditions"][0]["evaluator"]["type"] == "lt"


def test_gateway_metrics_silence_rule_uses_heartbeat_counter() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-gateway-metrics-silent"]
    assert rule_query_exprs(rule, "prometheus") == [
        "absent_over_time(meiva_gateway_latency_count_total[5m])"
    ]
    assert threshold_params(rule) == [[0]]
    assert rule["for"] == "5m"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"


def _manifest_cron_interval_minutes(name: str) -> int:
    manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "schedules/manifest.json").read_text(
            encoding="utf-8"
        )
    )
    (entry,) = [item for item in manifest["builtin_schedules"] if item["name"] == name]
    interval = re.fullmatch(r"\*/(\d+) \* \* \* \*", entry["cron_expr"])
    assert interval, f"{name}: cadence {entry['cron_expr']!r} is not a plain every-N-minutes cron"
    return int(interval.group(1))


def test_main_red_watch_silence_rule_waits_for_two_missed_rounds() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-main-red-watch-silent"]
    (expr,) = rule_query_exprs(rule, "prometheus")
    assert rule_query_exprs(rule, "loki") == []
    metric = "meiva_main_red_watch_round_rounds_total"
    window = re.match(rf"absent_over_time\({metric}\[(\d+)h\]\) and on\(\) ", expr)
    assert window, expr
    assert int(window.group(1)) * 60 == 2 * _manifest_cron_interval_minutes("main-red-watch")
    assert f"count(last_over_time({metric}[6d])) > 0" in expr, "a never-seen watch must not alert"
    assert threshold_params(rule) == [[0]]
    assert rule["for"] == "5m"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"
    assert rule["labels"] == {
        "severity": "error",
        "ruleUID": "meiva-ops-main-red-watch-silent",
        "metric": "main_red_watch_silence",
        "team": "meiva-ops",
    }


def test_root_health_tick_staleness_tracks_each_recent_root_home() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-root-health-stale"]
    expr = rule_query_exprs(rule, "prometheus")[0]
    assert "meiva_root_health_tick_last_tick_timestamp_seconds" in expr
    assert "max_over_time" in expr
    assert "meiva_root_health_expected_expected_since_timestamp_seconds[24h]" in expr
    assert "[3m]" in expr
    assert "unless on(machine, home_id, process)" in expr
    assert "max by (machine, home_id, process)" in expr
    assert threshold_params(rule) == [[0]]
    assert rule["for"] == "5m"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"


def test_latency_rule_uses_prometheus_histogram() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    exprs = rule_query_exprs(rules["meiva-ops-llm-latency-p95"], "prometheus")
    assert len(exprs) == 1
    expr = exprs[0]
    assert "histogram_quantile(0.95" in expr
    assert "meiva_llm_usage_latency_milliseconds_bucket" in expr
    assert "[10m]" in expr
    assert threshold_params(rules["meiva-ops-llm-latency-p95"]) == [[60000]]


def test_checkpoint_blobs_freshness_guard() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-checkpoint-blobs-freshness"]
    exprs = rule_query_exprs(rule, "prometheus")
    assert exprs == [
        "absent_over_time(meiva_checkpoint_table_sizes_checkpoints_bytes_ratio[2h])"
        " or absent_over_time(meiva_checkpoint_table_sizes_writes_bytes_ratio[2h])"
        " or absent_over_time(meiva_checkpoint_table_sizes_blobs_bytes_ratio[2h])"
    ]
    assert rule_query_exprs(rule, "loki") == []
    assert rule["for"] == "5m"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"
    assert threshold_params(rule) == [[0]]
    assert rule["labels"] == {
        "severity": "warning",
        "ruleUID": "meiva-ops-checkpoint-blobs-freshness",
        "metric": "checkpoint_blobs_freshness",
        "team": "meiva-ops",
    }
    description = rule["annotations"]["description"]
    assert "events_maintenance" in description
    assert "blind" in description


def test_checkpoint_blobs_growth_tier() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-checkpoint-blobs-growth"]
    exprs = rule_query_exprs(rule, "prometheus")
    assert exprs == [
        "(max(meiva_checkpoint_table_sizes_checkpoints_bytes_ratio) + "
        "max(meiva_checkpoint_table_sizes_writes_bytes_ratio) + "
        "max(meiva_checkpoint_table_sizes_blobs_bytes_ratio)) - "
        "(max(meiva_checkpoint_table_sizes_checkpoints_bytes_ratio offset 6h) + "
        "max(meiva_checkpoint_table_sizes_writes_bytes_ratio offset 6h) + "
        "max(meiva_checkpoint_table_sizes_blobs_bytes_ratio offset 6h))"
    ]
    assert rule_query_exprs(rule, "loki") == []
    assert rule["for"] == "15m"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "OK"
    assert threshold_params(rule) == [[1073741824]]
    assert rule["labels"] == {
        "severity": "warning",
        "ruleUID": "meiva-ops-checkpoint-blobs-growth",
        "metric": "checkpoint_blobs_growth",
        "team": "meiva-ops",
    }
    description = rule["annotations"]["description"]
    assert "2026-10-17" in description
    assert "full" in description.lower()
    assert "disk-watermark" in description


_UNIT_RENDER = {"1": "_ratio", "ms": "_milliseconds", "s": "_seconds"}
_KIND_EXTENSIONS = {
    "counter": ("_total",),
    "histogram": ("_bucket", "_count", "_sum"),
}


def _rendered_names(event: str, field: str) -> set[str]:
    kind = METRIC_DISPOSITION.get(event, {}).get(field)
    if kind is None:
        return set()
    base = f"meiva_{event}_{strip_unit_suffix(field)}"
    unit = unit_for(field)
    if kind == "gauge":
        return {base + _UNIT_RENDER[unit]}
    suffix = "" if unit == "1" else _UNIT_RENDER[unit]
    if kind == "counter":
        name = base + suffix
        return {name if name.endswith("_total") else name + "_total"}
    return {base + suffix + extension for extension in _KIND_EXTENSIONS[kind]}


def test_prometheus_rule_and_dashboard_references_match_explicit_exports() -> None:
    emitted: set[str] = set()
    for event, fields in METRIC_DISPOSITION.items():
        assert fields.keys() <= set(payload_keys(event) or ()), event
        for field in fields:
            emitted.update(_rendered_names(event, field))

    rule_exprs = [expr for rule in load_rules() for expr in rule_query_exprs(rule, "prometheus")]
    dashboard = (
        Path(__file__).resolve().parents[2]
        / "deploy/lgtm/config/grafana/provisioning/dashboards/meiva-ops-main.json"
    )
    dashboard_exprs = [
        target["expr"]
        for panel in json.loads(dashboard.read_text(encoding="utf-8"))["panels"]
        for target in panel.get("targets", [])
        if target.get("datasource", panel.get("datasource", {})).get("uid") == "prometheus"
    ]
    referenced = {
        token
        for expr in [*rule_exprs, *dashboard_exprs]
        for token in re.findall(r"\bmeiva_\w+", _strip_string_literals(expr))
    }
    assert rule_exprs and dashboard_exprs and referenced
    assert referenced <= emitted, sorted(referenced - emitted)


def test_unit_one_gauges_render_with_the_ratio_suffix() -> None:
    assert _rendered_names("checkpoint_table_sizes", "blobs_bytes") == {
        "meiva_checkpoint_table_sizes_blobs_bytes_ratio"
    }
    assert _rendered_names("event_log_drop", "last_dropped_at") == {
        "meiva_event_log_drop_last_dropped_at_ratio"
    }
    assert _rendered_names("memory_search_stats", "rows") == {
        "meiva_memory_search_stats_rows_ratio"
    }
    assert _rendered_names("gateway_latency", "count") == {"meiva_gateway_latency_count_total"}
    assert _rendered_names("main_red_watch_round", "rounds") == {
        "meiva_main_red_watch_round_rounds_total"
    }
    assert _rendered_names("turn_end", "duration_seconds") == {
        "meiva_turn_end_duration_seconds_bucket",
        "meiva_turn_end_duration_seconds_count",
        "meiva_turn_end_duration_seconds_sum",
    }


def test_inbound_consumed_unknown_rule_counts_the_event_over_15m() -> None:
    rules = {r["uid"]: r for r in load_rules()}
    rule = rules["meiva-ops-inbound-consumed-unknown"]
    exprs = rule_query_exprs(rule, "loki")
    assert len(exprs) == 2
    for expr in exprs:
        assert "count_over_time" in expr
        assert 'event_name="inbound_consumed_unknown"' in expr
        assert "[15m]" in expr
    assert "(24h)" not in rule["title"]
    assert "24h" not in rule["annotations"]["summary"]
    assert rule["for"] == "5m"
    assert threshold_params(rule) == [[2]]
    assert rule["labels"] == {
        "severity": "warning",
        "ruleUID": "meiva-ops-inbound-consumed-unknown",
        "metric": "inbound_consumed_unknown",
        "team": "meiva-ops",
    }


# meiva:note
# The dead-man pair is judged as a pair: the watchdog's period (5m, its route's
# repeat_interval) is the N the silence rule multiplies by three, and the gauge it
# reads is the one the gateway's ingest refreshes. Pinning only one half lets the
# other drift into a switch that never fires -- or fires on a quiet night. The silent
# rule's no-data state stays OK because the gauge rides the gateway's own OTLP push: a
# stopped gateway takes it along, so absence would report the gateway (the metrics-silent
# rule's subject) and would page on every collector restart and fresh install.
def test_alert_pipeline_watchdog_and_silence_rule_agree_on_the_period() -> None:
    rules = {rule["uid"]: rule for rule in load_rules()}
    watchdog = rules["meiva-ops-alert-pipeline-watchdog"]
    silent = rules["meiva-ops-alert-pipeline-silent"]
    assert watchdog["labels"]["metric"] == "alert_pipeline_watchdog"
    assert watchdog["labels"]["notify_im"] == "false"
    assert watchdog["for"] == "0m"
    assert rule_query_exprs(watchdog, "prometheus") == ["vector(1)"]
    assert silent["labels"]["metric"] == "alert_pipeline_silent"
    assert rule_query_exprs(silent, "prometheus") == [
        "time() - max(meiva_alerts_last_ingest_timestamp_seconds)"
    ]
    assert silent["noDataState"] == "OK"
    assert silent["execErrState"] == "OK"
    (threshold,) = threshold_params(silent)
    assert threshold == [900]


# meiva:note
# The Alerts page shows the summary line, so a rule about one agent must name it there. These
# rules group by the envelope agent id. Grafana renders a label the series lacks as "[no value]",
# so rules whose event family gained its agent id at the call site (older events carry none) guard
# the label and render "unknown" instead.
_PER_AGENT_RULES = {
    "meiva-ops-llm-turn-aborted": True,
    "meiva-ops-exec-node-timeout": True,
    "meiva-ops-exec-subprocess-killed": True,
    "meiva-ops-exec-child-boot-failed": False,
    "meiva-ops-delivery-stalled": False,
    "meiva-ops-delivery-stranded-wake": False,
}
_GUARDED_AGENT = "{{ with $labels.agent_id }}{{ . }}{{ else }}unknown{{ end }}"


@pytest.mark.parametrize("uid", sorted(_PER_AGENT_RULES))
def test_a_per_agent_rule_groups_by_and_names_its_agent(uid: str) -> None:
    rule = {r["uid"]: r for r in load_rules()}[uid]
    (expr,) = [" ".join(e.split()) for e in rule_query_exprs(rule, "loki")[:1]]
    assert re.match(r"(sum|max) by \([^)]*\bagent_id\b", expr), expr
    summary = rule["annotations"]["summary"]
    assert "$labels.agent_id" in summary
    assert (_GUARDED_AGENT in summary) is _PER_AGENT_RULES[uid]


# meiva:note
# Fleet-volume rules keep their fleet-wide threshold (one host crash or stall touches many agents at
# once) and report how many distinct agents the volume spans: query B counts agent ids over A's
# window, `or vector(0)` keeps it a number when no row carries an agent id, and reduce N is what
# the summary interpolates.
_AGENT_COUNT_RULES = (
    "meiva-ops-sse-drop-backlog",
    "meiva-ops-delivery-stalled-backlog",
    "meiva-ops-inbound-consumed-unknown",
)


@pytest.mark.parametrize("uid", _AGENT_COUNT_RULES)
def test_a_fleet_volume_rule_reports_its_distinct_agents(uid: str) -> None:
    rule = {r["uid"]: r for r in load_rules()}[uid]
    nodes = {d["refId"]: d for d in rule["data"]}
    count = " ".join(nodes["B"]["model"]["expr"].split())
    assert count.startswith("count(") and count.endswith(" or vector(0)"), count
    assert re.search(r"by \([^)]*\bagent_id\b", count), count
    windows = re.compile(r"\[(\d+[smhd])\]")
    assert windows.findall(count) == windows.findall(nodes["A"]["model"]["expr"]), count
    assert nodes["B"]["relativeTimeRange"] == nodes["A"]["relativeTimeRange"]
    assert nodes["B"]["datasourceUid"] == "loki"
    assert nodes["N"]["model"]["type"] == "reduce"
    assert nodes["N"]["model"]["expression"] == "B"
    assert "{{ $values.N }} agent(s)" in rule["annotations"]["summary"]
    assert rule["condition"] == "D" and nodes["D"]["model"]["expression"] == "C"


# meiva:note
# Grafana renders a `$labels.X` the series does not carry as "[no value]" in the summary that reaches
# the Alerts page, so every label a rule's text names must be one its first query keeps.
def test_notification_text_names_only_labels_the_first_query_keeps() -> None:
    for rule in load_rules():
        expr = " ".join(rule["data"][0]["model"]["expr"].split())
        kept = {
            name.strip() for part in re.findall(r"by \(([^)]*)\)", expr) for name in part.split(",")
        }
        text = rule["annotations"]["summary"] + rule["annotations"].get("description", "")
        for label in re.findall(r"\$labels\.(\w+)", text):
            assert label in kept, f"{rule['uid']} names $labels.{label} but keeps {kept}"


# meiva:note
# A release wave cuts running turns on purpose; the host marks those aborts `cause=planned_restart`
# and the turn-aborted alert leaves exactly that value out, so an abort without a cause still alerts.
# The value is the one `agent.turn.runloop.PLANNED_RESTART` emits (pinned beside the emitter in
# `test_circuit_breaker.py`).
def test_the_turn_aborted_alert_leaves_out_only_planned_restarts() -> None:
    rule = {r["uid"]: r for r in load_rules()}["meiva-ops-llm-turn-aborted"]
    (expr,) = [" ".join(e.split()) for e in rule_query_exprs(rule, "loki")]
    assert '| attributes_cause != "planned_restart" [15m]' in expr
    assert expr.count("attributes_cause") == 1


@pytest.mark.parametrize(
    ("uid", "metric", "pending"),
    [
        ("meiva-ops-schedule-stalled", "meiva_schedule_manager_state_stalled_ratio", "0m"),
        (
            "meiva-ops-schedule-process-lost",
            "meiva_schedule_manager_state_process_lost_ratio",
            "0m",
        ),
        (
            "meiva-ops-schedule-convergence-abandoned",
            "meiva_schedule_manager_state_convergence_abandoned_ratio",
            "5m",
        ),
    ],
)
def test_schedule_state_rules_read_prometheus_gauges(uid: str, metric: str, pending: str) -> None:
    rule = {r["uid"]: r for r in load_rules()}[uid]
    assert rule_query_exprs(rule, "loki") == []
    assert rule_query_exprs(rule, "prometheus") == [f"max by (machine) ({metric})"]
    assert rule["for"] == pending
    assert threshold_params(rule) == [[0]]
    assert "{{ $labels.machine }}" in rule["annotations"]["summary"]


_STRING_LITERAL_RE = re.compile(r'"(?:\\.|[^"\\])*"|`[^`]*`')
_PROM_CLUSTER_MATCHER_RE = re.compile(r"\bcluster\s*(?:=|!=|=~|!~)")
_DURATION_ATOM_RE = re.compile(r"(\d+)(ms|[smhdwy])")
_DURATION_UNITS_S: dict[str, float] = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 7 * 86400.0,
    "y": 365 * 86400.0,
}
_DURATION_PATTERN = r"(?:\d+(?:ms|[smhdwy]))+"
_LOGQL_RANGE_RE = re.compile(
    rf"\[({_DURATION_PATTERN})\](?:\s*\)*\s*offset\s+({_DURATION_PATTERN}))?"
)
_LOGQL_RANGE_FN_RE = re.compile(
    r"\b(?:count_over_time|sum_over_time|rate|rate_counter|bytes_over_time|bytes_rate|"
    r"max_over_time|min_over_time|avg_over_time|quantile_over_time|stdvar_over_time|"
    r"stddev_over_time|first_over_time|last_over_time)\s*\("
)
_MAX_LOKI_RANGE_WINDOW_S = 15 * 60


def _strip_string_literals(expr: str) -> str:
    return _STRING_LITERAL_RE.sub('""', expr)


def _parse_duration_s(raw: str) -> float:
    atoms = _DURATION_ATOM_RE.findall(raw)
    assert atoms and "".join(f"{n}{u}" for n, u in atoms) == raw, f"invalid duration: {raw!r}"
    return sum(int(n) * _DURATION_UNITS_S[u] for n, u in atoms)


def _parse_logql_ranges(expr: str) -> list[tuple[float, float]]:
    stripped = _strip_string_literals(expr)
    ranges: list[tuple[float, float]] = []
    for win_raw, off_raw in _LOGQL_RANGE_RE.findall(stripped):
        window_s = _parse_duration_s(win_raw)
        offset_s = _parse_duration_s(off_raw) if off_raw else 0.0
        ranges.append((window_s, offset_s))
    return ranges


def test_logql_range_parser_handles_literals_compound_durations_and_outer_offset() -> None:
    assert _parse_logql_ranges(
        'sum(count_over_time({event_name="x"} | attributes_msg="[24h]" [15m]))'
    ) == [(900.0, 0.0)]
    assert _parse_logql_ranges('sum(count_over_time({event_name="x"}[15m]) offset 5m)') == [
        (900.0, 300.0)
    ]
    assert _parse_logql_ranges("rate({a=`[2w]`}[1h30m] offset 500ms)") == [(5400.0, 0.5)]


def test_prometheus_rules_never_filter_by_unpromoted_cluster_label() -> None:
    violations: list[str] = []
    for rule in load_rules():
        for query in rule["data"]:
            if query.get("datasourceUid") != "prometheus":
                continue
            expr = " ".join(str(query["model"].get("expr", "")).split())
            if _PROM_CLUSTER_MATCHER_RE.search(_strip_string_literals(expr)):
                violations.append(f"{rule['uid']}[{query['refId']}]: {expr}")
    assert not violations, (
        "Prometheus OTLP does not promote the resource `cluster` attribute to series labels; "
        "filtering by `cluster=` returns an empty vector and silently disables the alert: "
        + "; ".join(violations)
    )


def test_loki_range_aggregation_windows_do_not_exceed_15m() -> None:
    violations: list[str] = []
    for rule in load_rules():
        for query in rule["data"]:
            if query.get("datasourceUid") != "loki":
                continue
            expr = " ".join(str(query["model"].get("expr", "")).split())
            stripped = _strip_string_literals(expr)
            if not _LOGQL_RANGE_FN_RE.search(stripped):
                continue
            ranges = _parse_logql_ranges(expr)
            assert ranges, (
                f"{rule['uid']}[{query['refId']}]: range function without parseable window: {expr}"
            )
            for window_s, _ in ranges:
                if window_s > _MAX_LOKI_RANGE_WINDOW_S:
                    violations.append(
                        f"{rule['uid']}[{query['refId']}]: window {window_s:g}s > {_MAX_LOKI_RANGE_WINDOW_S}s"
                    )
    assert not violations, (
        "Loki range-aggregation alert windows must stay <= 15m (use a Prometheus state gauge "
        "for long-lived conditions): " + "; ".join(violations)
    )


def test_loki_rule_relative_time_range_matches_logql_window() -> None:
    mismatches: list[str] = []
    for rule in load_rules():
        for query in rule["data"]:
            if query.get("datasourceUid") != "loki":
                continue
            expr = " ".join(str(query["model"].get("expr", "")).split())
            ranges = _parse_logql_ranges(expr)
            assert ranges, (
                f"{rule['uid']}[{query['refId']}]: Loki query has no range selector: {expr}"
            )
            expected = set(ranges)
            assert len(expected) == 1, (
                f"{rule['uid']}[{query['refId']}]: conflicting range selectors {ranges}: {expr}"
            )
            window_s, offset_s = ranges[0]
            rtr = query.get("relativeTimeRange", {})
            actual = (rtr.get("from"), rtr.get("to"))
            wanted = (window_s + offset_s, offset_s)
            if actual != wanted:
                mismatches.append(
                    f"{rule['uid']}[{query['refId']}]: relativeTimeRange={actual} != expected {wanted}"
                )
    assert not mismatches, (
        "Loki relativeTimeRange must match LogQL [window] (+ offset): " + "; ".join(mismatches)
    )
