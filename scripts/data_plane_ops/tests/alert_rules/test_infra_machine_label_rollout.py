from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.data_plane_ops.tests.alert_rules.rule_helpers import (
    INFRA_RULE_METRICS,
    RULES,
    load_rules,
    rule_query_exprs,
)


def _promql_case(
    expr: str, expected: dict[str, float], *, exporter: bool = False
) -> dict[str, object]:
    extra_labels = ',exporter="otlphttp/loki"' if exporter else ""
    return {
        "expr": expr,
        "eval_time": "6m",
        "exp_samples": [
            {"labels": f'{{machine="{machine}"{extra_labels}}}', "value": value}
            for machine, value in expected.items()
        ],
    }


def test_infra_queries_survive_old_new_and_overlapping_collector_samples(tmp_path: Path) -> None:
    promtool = os.environ.get("MEIVA_TEST_PROMTOOL_BIN")
    if not promtool:
        pytest.skip("set MEIVA_TEST_PROMTOOL_BIN to evaluate the shipped PromQL with Prometheus")

    rules = {rule["uid"]: rule for rule in load_rules()}
    [silence] = rule_query_exprs(rules["meiva-ops-otelcol-host-silent"], "prometheus")
    [memory] = rule_query_exprs(rules["meiva-ops-host-memory-pressure"], "prometheus")
    [enqueue_failures] = rule_query_exprs(rules["meiva-ops-otelcol-enqueue-failures"], "prometheus")
    dashboard = RULES.parent.parent / "dashboards" / "meiva-ops-main.json"
    panels = json.loads(dashboard.read_text(encoding="utf-8"))["panels"]
    [memory_panel] = [panel for panel in panels if panel["id"] == 2102]
    [memory_target] = memory_panel["targets"]

    uptime = "otelcol_process_uptime_total"
    ram = "system_memory_utilization_ratio"
    drops = "otelcol_exporter_enqueue_failed_log_records_total"
    series = [
        (f'{uptime}{{machine_name="old-down"}}', "600"),
        (f'{uptime}{{machine="new-down"}}', "300"),
        (f'{uptime}{{machine_name="both-down"}}', "900"),
        (f'{uptime}{{machine="both-down",machine_name="both-down"}}', "450"),
        (f'{uptime}{{machine_name="upgraded"}}', "600"),
        (f'{uptime}{{machine="upgraded"}}', "_ _ _ _ _ _ 150"),
        (f'{uptime}{{machine="rolled-back"}}', "600"),
        (f'{uptime}{{machine_name="rolled-back"}}', "_ _ _ _ _ _ 150"),
        (f'{ram}{{machine_name="old",state="used"}}', "_ _ _ _ _ _ 0.96"),
        (f'{ram}{{machine="new",state="used"}}', "_ _ _ _ _ _ 0.94"),
        (f'{ram}{{machine_name="both",state="used"}}', "_ _ _ _ _ _ 0.99"),
        (f'{ram}{{machine="both",machine_name="both",state="used"}}', "_ _ _ _ _ _ 0.22"),
        (f'{ram}{{state="used"}}', "_ _ _ _ _ _ 0.99"),
        (f'{drops}{{machine_name="old",exporter="otlphttp/loki"}}', "0+5x6"),
        (f'{drops}{{machine="new",exporter="otlphttp/loki"}}', "0+1x6"),
        (f'{drops}{{machine_name="both",exporter="otlphttp/loki"}}', "0+5x6"),
        (f'{drops}{{machine="both",machine_name="both",exporter="otlphttp/loki"}}', "0+1x6"),
        (f'{drops}{{machine_name="new-one-sample",exporter="otlphttp/loki"}}', "0+5x6"),
        (f'{drops}{{machine="new-one-sample",exporter="otlphttp/loki"}}', "_ _ _ _ _ _ 1"),
    ]
    expected_memory = {"old": 0.96, "new": 0.94, "both": 0.22}
    cases = [
        _promql_case(silence, {"old-down": 600, "new-down": 300, "both-down": 450}),
        _promql_case(memory, expected_memory),
        _promql_case(memory_target["expr"], expected_memory),
        _promql_case(
            enqueue_failures, {"old": 25, "new": 5, "both": 5, "new-one-sample": 25}, exporter=True
        ),
    ]
    infra_uids = (
        *INFRA_RULE_METRICS,
        "meiva-ops-otelcol-queue-pressure",
        "meiva-ops-otelcol-enqueue-failures",
        "meiva-ops-otelcol-host-silent",
    )
    expressions = {
        expr for uid in infra_uids for expr in rule_query_exprs(rules[uid], "prometheus")
    }
    expressions.update(
        target["expr"]
        for panel in panels
        if 2101 <= panel["id"] <= 2112
        for target in panel["targets"]
    )
    file = tmp_path / "rollout.yml"
    file.write_text(
        yaml.safe_dump(
            {
                "tests": [
                    {
                        "interval": "1m",
                        "input_series": [
                            {"series": labels, "values": values} for labels, values in series
                        ],
                        "promql_expr_test": cases,
                    },
                    {
                        "interval": "1m",
                        "input_series": [],
                        "promql_expr_test": [
                            _promql_case(expr, {}) for expr in sorted(expressions)
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 - operator supplies the native promtool test binary
        [promtool, "test", "rules", str(file)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
