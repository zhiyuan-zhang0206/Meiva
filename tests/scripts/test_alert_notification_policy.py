from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ALERTING = Path(__file__).resolve().parents[2] / "deploy/lgtm/config/grafana"
_CONTACT = _ALERTING / "provisioning/alerting/contact.yml"
_RULES = _ALERTING / "provisioning/alerting/rules.yml"


def _document() -> dict[str, Any]:
    return yaml.safe_load(_CONTACT.read_text(encoding="utf-8"))


def _policy() -> dict[str, Any]:
    (policy,) = _document()["policies"]
    return policy


def test_the_only_receiver_is_the_gateway_webhook() -> None:
    contacts = {c["name"]: c for c in _document()["contactPoints"]}
    assert list(contacts) == ["meiva-alerts-webhook"]
    (receiver,) = contacts["meiva-alerts-webhook"]["receivers"]
    assert receiver["type"] == "webhook"
    assert receiver["disableResolveMessage"] is False
    policy = _policy()
    assert policy["receiver"] == "meiva-alerts-webhook"
    assert policy["matchers"] == []
    assert {route["receiver"] for route in policy["routes"]} == {"meiva-alerts-webhook"}


def test_instances_of_one_rule_are_merged_into_one_notification_group() -> None:
    policy = _policy()
    assert policy["group_by"] == ["alertname"]
    assert (policy["group_wait"], policy["group_interval"], policy["repeat_interval"]) == (
        "30s",
        "5m",
        "4h",
    )


def test_retired_receivers_are_removed() -> None:
    assert _document()["deleteContactPoints"] == [{"orgId": 1, "uid": "meiva-alerts-telegram"}]


def test_the_watchdog_is_the_only_route_and_does_not_fall_through() -> None:
    (route,) = _policy()["routes"]
    assert route["object_matchers"] == [["metric", "=", "alert_pipeline_watchdog"]]
    assert route["continue"] is False
    assert route["repeat_interval"] == "5m"
    assert route["group_interval"] == "1m"
    assert route["group_wait"] == "0s"


def test_the_silent_rule_judges_on_three_watchdog_periods() -> None:
    (route,) = _policy()["routes"]
    period_seconds = 5 * 60
    assert route["repeat_interval"] == "5m"
    rules = yaml.safe_load(_RULES.read_text(encoding="utf-8"))
    (silent,) = [
        rule
        for group in rules["groups"]
        for rule in group["rules"]
        if rule["uid"] == "meiva-ops-alert-pipeline-silent"
    ]
    (threshold,) = [
        condition["evaluator"]["params"]
        for step in silent["data"]
        for condition in step["model"].get("conditions", [])
    ]
    assert threshold == [3 * period_seconds]
