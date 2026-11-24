from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from base.packages.dashboard_supply import load_repo_plugin_specs
from base.telemetry.metrics.core import catalog
from base.telemetry.metrics.plugin_metrics import (
    MetricSpec,
    render_query,
    render_targets,
    render_title,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def registered_metrics() -> list[MetricSpec]:
    result = load_repo_plugin_specs()
    assert result.failed == []
    return result.specs


def _load_core() -> list[MetricSpec]:
    return catalog.collect_core_metrics()


def test_shipped_plugin_metrics_are_logql() -> None:
    specs = registered_metrics()
    assert len(specs) == 6
    names = {spec.name for spec in specs}
    assert {
        "meiva_memory_recall_search_latency_ms",
        "meiva_memory_recall_injected_notes",
    } <= names
    for spec in specs:
        assert spec.query_type == "logql", f"{spec.name} must read the Loki event stream"


def test_rendered_queries_target_the_event_stream() -> None:
    for spec in registered_metrics():
        for template in render_targets(spec):
            assert 'service_name="unknown_service"' in template, (
                f"{spec.name} lost the event-stream selector"
            )
            assert "| json" in template, f"{spec.name} lost the | json pipeline"
            assert "| event_name" not in template, (
                f"{spec.name} filters event_name after | json:\n{template}"
            )
            assert "| agent_id" not in template, (
                f"{spec.name} filters agent_id after | json:\n{template}"
            )


def test_dashboard_json_matches_registrations() -> None:
    specs = [s for s in registered_metrics() if "grafana" in s.output]
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/meiva-ops-main.json"
    data = json.loads(path.read_text())
    by_title = {p.get("title"): p for p in data["panels"]}
    for spec in specs:
        panel = by_title[render_title(spec)]
        assert panel["datasource"] == {"type": "loki", "uid": "loki"}
        targets = panel["targets"]
        assert [t["expr"] for t in targets] == render_targets(spec)
        qtype = "instant" if spec.panel == "stat" else "range"
        assert all(t["queryType"] == qtype for t in targets)


def test_plugin_panels_live_under_their_plugin_rows() -> None:
    specs = [s for s in registered_metrics() if "grafana" in s.output]
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/meiva-ops-main.json"
    data = json.loads(path.read_text())
    rows = sorted((p["gridPos"]["y"], p["title"]) for p in data["panels"] if p.get("type") == "row")
    by_title = {p.get("title"): p for p in data["panels"]}
    for spec in specs:
        y = by_title[render_title(spec)]["gridPos"]["y"]
        section = max(row for row in rows if row[0] <= y)[1]
        assert section == spec.plugin, (
            f"{render_title(spec)!r} sits under {section!r}, not its plugin row {spec.plugin!r}"
        )


def _assert_no_dashboard_panel_overlap(panels: list[dict[str, Any]]) -> None:
    for index, panel in enumerate(panels):
        grid = panel["gridPos"]
        for other in panels[index + 1 :]:
            other_grid = other["gridPos"]
            overlaps = (
                grid["x"] < other_grid["x"] + other_grid["w"]
                and other_grid["x"] < grid["x"] + grid["w"]
                and grid["y"] < other_grid["y"] + other_grid["h"]
                and other_grid["y"] < grid["y"] + grid["h"]
            )
            assert not overlaps, (
                f"dashboard gridPos overlap: panel {panel['id']} ({panel['title']!r}) and "
                f"panel {other['id']} ({other['title']!r})"
            )


def _assert_sql_panel_matches_spec(panel: dict[str, Any], expected: list[str]) -> None:
    targets = panel["targets"]
    assert panel["datasource"] == {"type": "postgres", "uid": "ops"}
    assert [target["rawSql"] for target in targets] == expected
    assert all("queryType" not in target for target in targets)


def _assert_promql_panel_matches_spec(
    panel: dict[str, Any], spec: MetricSpec, expected: list[str]
) -> None:
    targets = panel["targets"]
    assert panel["datasource"] == {"type": "prometheus", "uid": "prometheus"}
    assert [target["expr"] for target in targets] == expected
    if spec.panel in {"stat", "table"}:
        assert all(
            target.get("instant") is True and target.get("range") is False for target in targets
        )
    else:
        assert all(target["queryType"] == "range" for target in targets)
    assert spec.target_names is not None
    assert [target["legendFormat"] for target in targets] == spec.target_names


def _assert_loki_panel_matches_spec(
    panel: dict[str, Any], spec: MetricSpec, expected: list[str]
) -> None:
    targets = panel["targets"]
    assert panel["datasource"] == {"type": "loki", "uid": "loki"}
    assert [target["expr"] for target in targets] == expected
    expected_query_type = (
        "instant"
        if spec.query_type == "logql"
        and spec.grafana_max_lines is None
        and (spec.panel in {"stat", "table"} or "$__range" in expected[0])
        else "range"
    )
    assert all(target["queryType"] == expected_query_type for target in targets)


def _assert_dashboard_panel_matches_spec(panel: dict[str, Any], spec: MetricSpec) -> None:
    expected = render_targets(spec)
    if spec.query_type == "sql":
        _assert_sql_panel_matches_spec(panel, expected)
    elif spec.query_type == "promql":
        _assert_promql_panel_matches_spec(panel, spec, expected)
    else:
        _assert_loki_panel_matches_spec(panel, spec, expected)


def test_dashboard_json_matches_core_registrations() -> None:
    specs = [spec for spec in _load_core() if "grafana" in spec.output]
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/meiva-ops-main.json"
    data = json.loads(path.read_text())
    panels_by_title = {panel.get("title"): panel for panel in data["panels"]}

    assert len(panels_by_title) == len(data["panels"]), "dashboard panel titles must be unique"
    _assert_no_dashboard_panel_overlap(data["panels"])
    for spec in specs:
        _assert_dashboard_panel_matches_spec(panels_by_title[render_title(spec)], spec)


def test_dashboard_has_142_loki_targets() -> None:
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/meiva-ops-main.json"
    panels = json.loads(path.read_text())["panels"]
    loki_targets = [
        target
        for panel in panels
        for target in panel.get("targets", [])
        if target.get("datasource", panel.get("datasource", {})).get("uid") == "loki"
    ]
    assert len(loki_targets) == 142


def test_agent_max_id_gauge_names_match_the_otlp_contract() -> None:

    from base.telemetry.otlp.telemetry_otlp import METRIC_DISPOSITION, strip_unit_suffix

    specs = {spec.name: spec for spec in _load_core()}
    assert METRIC_DISPOSITION["agent_registry"]["max_id"] == "gauge"
    gauge = f"meiva_agent_registry_{strip_unit_suffix('max_id')}_ratio"
    assert specs["core_agent_max_id"].query == f"max({gauge})"
    assert specs["core_agent_max_id"].query_type == "promql"
    assert specs["core_agent_max_id"].panel == "timeseries"
    assert specs["core_agent_max_id_growth_rate"].query_type == "promql"
    assert f"deriv(max({gauge})[1h:])" in (specs["core_agent_max_id_growth_rate"].query)


def test_pr_flow_table_uses_the_latest_event_for_each_day_and_percentile() -> None:
    latency = next(spec for spec in _load_core() if spec.name == "core_pr_flow_ready_to_merge")
    assert latency.query_type == "logql"
    assert latency.panel == "table"
    assert latency.unit == "s"
    query = render_query(latency)
    for percentile in ("median", "p90"):
        assert f"attributes.ready_to_merge_{percentile}_seconds" in query
        assert f'label_format percentile="{percentile}"' in query
    assert query.count('event_name="pr_flow_daily"') == 2
    assert query.count("last_over_time(") == 2
    assert query.count("[26h]) by (day, percentile)") == 2
    assert latency.transformations == [
        {"id": "labelsToFields", "options": {}},
        {
            "id": "groupingToMatrix",
            "options": {
                "columnField": "percentile",
                "rowField": "day",
                "valueField": "Value #A",
            },
        },
        {"id": "organize", "options": {"renameByName": {"day\\percentile": "day"}}},
    ]


def test_dashboard_legends_and_time_ranges_are_explicit() -> None:
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/meiva-ops-main.json"
    panels = json.loads(path.read_text())["panels"]
    for panel in panels:
        for target in panel.get("targets", []):
            datasource = target.get("datasource", panel.get("datasource", {}))
            if datasource.get("uid") == "loki":
                assert target.get("legendFormat"), (panel["id"], target.get("refId"))
        assert not any(
            override.get("matcher", {}).get("id") == "byName"
            and any(property_.get("id") == "displayName" for property_ in override["properties"])
            for override in panel.get("fieldConfig", {}).get("overrides", [])
        ), panel["id"]

    assert all("timeFrom" not in panel for panel in panels)
    assert all("interval" not in panel for panel in panels)


_COST_WINDOW_TILES = (
    "LLM cost (window)",
    "Tokens (window)",
    "LLM cost estimate — day pace",
    "LLM cost estimate — 30-day pace",
    "LLM cost / minute",
)
_COST_DESCRIBED_TILES = (
    "LLM cost estimate — day pace",
    "LLM cost estimate — 30-day pace",
    "LLM cost / minute",
    "LLM cost by model (Top 20)",
    "LLM cost by agent (Top 20)",
)


def _assert_llm_usage_panels_filter_telemetry(panels: list[dict[str, Any]]) -> None:
    for panel in panels:
        expressions = [target.get("expr", "") for target in panel.get("targets", [])]
        if any('event_name="llm_usage"' in expression for expression in expressions):
            assert all('category="telemetry"' in expression for expression in expressions)


def test_cost_dashboard_windows_and_telemetry_contract() -> None:
    path = _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/meiva-ops-main.json"
    data = json.loads(path.read_text())
    by_title = {panel.get("title"): panel for panel in data["panels"]}

    assert data["timezone"] == "Asia/Shanghai"
    assert all(panel["collapsed"] is False for panel in data["panels"] if panel["type"] == "row")
    for title in _COST_WINDOW_TILES:
        assert "timeFrom" not in by_title[title]
        assert "interval" not in by_title[title]

    core_by_title = {render_title(spec): spec for spec in _load_core()}
    for title in _COST_DESCRIBED_TILES:
        assert by_title[title]["description"] == core_by_title[title].description

    _assert_llm_usage_panels_filter_telemetry(data["panels"])
