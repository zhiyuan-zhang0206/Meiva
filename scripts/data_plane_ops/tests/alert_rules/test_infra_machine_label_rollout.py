from __future__ import annotations

import json

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


def _infra_queries() -> tuple[dict[str, str], dict[tuple[int, str], str]]:
    rules = {rule["uid"]: rule for rule in load_rules()}
    infra_uids = (
        *INFRA_RULE_METRICS,
        "meiva-ops-otelcol-queue-pressure",
        "meiva-ops-otelcol-enqueue-failures",
        "meiva-ops-otelcol-host-silent",
    )
    rule_queries: dict[str, str] = {}
    for uid in infra_uids:
        [expr] = rule_query_exprs(rules[uid], "prometheus")
        rule_queries[uid] = expr

    dashboard = RULES.parent.parent / "dashboards" / "meiva-ops-main.json"
    panels = json.loads(dashboard.read_text(encoding="utf-8"))["panels"]
    panel_queries: dict[tuple[int, str], str] = {
        (panel["id"], target["refId"]): target["expr"]
        for panel in panels
        if 2101 <= panel["id"] <= 2112
        for target in panel["targets"]
    }
    return rule_queries, panel_queries


def test_infra_queries_prefer_current_samples_with_legacy_fallback() -> None:
    rules, panels = _infra_queries()
    assert panels
    for name, expr in (*rules.items(), *panels.items()):
        assert 'machine!=""' in expr, name
        assert 'machine="", machine_name!=""' in expr, name
        assert "or on (machine" in expr, name
        assert '", "machine_name")' in expr, name
        assert expr.count("or on (") == expr.count("label_join("), name


def promtool_fixture() -> dict[str, object]:
    rules, panels = _infra_queries()
    silence = rules["meiva-ops-otelcol-host-silent"]
    memory = rules["meiva-ops-host-memory-pressure"]
    enqueue_failures = rules["meiva-ops-otelcol-enqueue-failures"]
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
        _promql_case(panels[(2102, "A")], expected_memory),
        _promql_case(
            enqueue_failures, {"old": 25, "new": 5, "both": 5, "new-one-sample": 25}, exporter=True
        ),
    ]
    expressions = set(rules.values()) | set(panels.values())
    return {
        "tests": [
            {
                "interval": "1m",
                "input_series": [{"series": labels, "values": values} for labels, values in series],
                "promql_expr_test": cases,
            },
            {
                "interval": "1m",
                "input_series": [],
                "promql_expr_test": [_promql_case(expr, {}) for expr in sorted(expressions)],
            },
        ],
    }


if __name__ == "__main__":
    print(yaml.safe_dump(promtool_fixture()), end="")
