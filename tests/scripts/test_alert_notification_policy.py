from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_CONTACT = (
    Path(__file__).resolve().parents[2]
    / "deploy/lgtm/config/grafana/provisioning/alerting/contact.yml"
)


def _document() -> dict[str, Any]:
    return yaml.safe_load(_CONTACT.read_text(encoding="utf-8"))


def _policy() -> dict[str, Any]:
    (policy,) = _document()["policies"]
    return policy


# meiva:note
# Every route keyed by its single matcher (`label:value`), in policy order.
def _routes() -> dict[str, dict[str, Any]]:
    routes: dict[str, dict[str, Any]] = {}
    for route in _policy()["routes"]:
        label, _operator, value = route["object_matchers"][0]
        routes[f"{label}:{value}"] = route
    return routes


def test_every_alert_reaches_the_webhook_receiver() -> None:
    contacts = {c["name"]: c for c in _document()["contactPoints"]}
    (receiver,) = contacts["meiva-alerts-webhook"]["receivers"]
    assert receiver["type"] == "webhook"
    assert receiver["disableResolveMessage"] is False
    policy = _policy()
    assert policy["receiver"] == "meiva-alerts-webhook"
    assert policy["matchers"] == []
    assert _routes()["alertname:.+"]["object_matchers"] == [["alertname", "=~", ".+"]]
    assert list(_routes()) == [
        "attributes_check:gateway_liveness",
        "metric:health_probe_silent",
        "metric:root_unit_restart_attempts_exhausted",
        "metric:alert_pipeline_watchdog",
        "metric:alert_pipeline_silent",
        "severity:critical",
        "alertname:.+",
    ]


def test_instances_of_one_rule_are_merged_into_one_notification_group() -> None:
    policy = _policy()
    assert policy["group_by"] == ["alertname"]
    assert (policy["group_wait"], policy["group_interval"], policy["repeat_interval"]) == (
        "30s",
        "5m",
        "4h",
    )
    catch_all = _routes()["alertname:.+"]
    assert catch_all["group_by"] == ["alertname"]
    assert (catch_all["group_wait"], catch_all["group_interval"], catch_all["repeat_interval"]) == (
        "30s",
        "5m",
        "4h",
    )


def test_critical_alerts_skip_the_batching_wait() -> None:
    route = _routes()["severity:critical"]
    assert route["object_matchers"] == [["severity", "=", "critical"]]
    assert "continue" not in route
    assert (route["group_wait"], route["group_interval"], route["repeat_interval"]) == (
        "0s",
        "1m",
        "1h",
    )


# meiva:note
# Retirement retains webhook routes and prevents duplicate catch-all delivery.
def test_retired_receivers_are_removed_and_all_routes_use_the_webhook() -> None:
    document = _document()
    assert [c["name"] for c in document["contactPoints"]] == ["meiva-alerts-webhook"]
    assert document["deleteContactPoints"] == [{"orgId": 1, "uid": "meiva-alerts-telegram"}]
    assert {route["receiver"] for route in _policy()["routes"]} == {"meiva-alerts-webhook"}
    for key in (
        "attributes_check:gateway_liveness",
        "metric:health_probe_silent",
        "metric:root_unit_restart_attempts_exhausted",
        "metric:alert_pipeline_silent",
    ):
        assert _routes()[key]["continue"] is False


# meiva:note
# The watchdog is the only route that must NOT fall through: it repeats every five
# minutes, so a `continue: true` or a missing route would deliver it twice (once from
# its own route, once from the catch-all). Its period is the N the silent rule judges
# on, so both numbers are asserted together -- a change to one without the other is
# how a dead-man switch silently stops covering the gap it was built for.
def test_the_watchdog_repeats_at_the_period_the_silent_rule_judges_on() -> None:
    watchdog = _routes()["metric:alert_pipeline_watchdog"]
    silent = _routes()["metric:alert_pipeline_silent"]
    assert watchdog["receiver"] == "meiva-alerts-webhook"
    assert watchdog["continue"] is False
    assert watchdog["repeat_interval"] == watchdog["group_interval"] == "5m"
    assert watchdog["group_wait"] == "0s"
    assert silent["receiver"] == "meiva-alerts-webhook"
    assert silent["continue"] is False
