from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

RULES = (
    Path(__file__).resolve().parents[4]
    / "deploy/lgtm/config/grafana/provisioning/alerting/rules.yml"
)

INFRA_RULE_METRICS = {
    "meiva-ops-host-cpu-saturated": "system_cpu_utilization_ratio",
    "meiva-ops-host-memory-pressure": "system_memory_utilization_ratio",
    "meiva-ops-host-disk-watermark": "system_filesystem_utilization_ratio",
    "meiva-ops-host-disk-watermark-93": "system_filesystem_utilization_ratio",
    "meiva-ops-host-disk-watermark-95": "system_filesystem_utilization_ratio",
    "meiva-ops-pg-connection-saturation": "postgresql_backends",
    "meiva-ops-redis-memory": "redis_memory_used_bytes",
}


def load_groups() -> list[dict[str, Any]]:
    doc = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    assert doc is not None
    groups = doc["groups"]
    assert len(groups) == 2
    assert [group["name"] for group in groups] == ["meiva-ops", "meiva-ops-slow"]
    assert [group["folder"] for group in groups] == ["Meiva", "Meiva"]
    assert [group["interval"] for group in groups] == ["1m", "5m"]
    assert [len(group["rules"]) for group in groups] == [54, 14]
    return groups


def load_rules() -> list[dict[str, Any]]:
    groups = load_groups()
    return [rule for group in groups for rule in group["rules"]]


def rule_query_exprs(rule: dict[str, Any], datasource_uid: str) -> list[str]:
    return [
        d["model"]["expr"]
        for d in rule["data"]
        if d.get("datasourceUid") == datasource_uid and d.get("queryType") == ""
    ]


def threshold_params(rule: dict[str, Any]) -> list[list[Any]]:
    return [
        d["model"]["conditions"][0]["evaluator"]["params"]
        for d in rule["data"]
        if d["model"].get("type") == "threshold"
    ]


def assert_latency_query_scopes_to_route_class(expr: str, route_class: str) -> None:
    assert 'event_name="gateway_latency"' in expr
    assert f'| attributes_route_class="{route_class}"' in expr
    assert "attributes_route !~" not in expr
    assert "unwrap attributes_p95_ms" in expr
    assert "max by (attributes_route)" in expr


def assert_reduce_keeps_route_label(rule: dict[str, Any]) -> None:
    reduce_node = next(d for d in rule["data"] if d["model"].get("type") == "reduce")
    assert reduce_node["model"].get("mode") == "byLabels"
    assert "attributes_route" in reduce_node["model"].get("includeLabels", [])
