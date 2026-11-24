# meiva:note
# The alert-copy contract (Task #1261, user ruling 2026-08-13): the user-visible
# alert templates live in base/telemetry/alerts/copy.py -- no head/trigger/jump-link
# literals in base/telemetry/alerts/__init__.py -- and the copy is English only
# (user ruling 2026-11-24): alert labels/annotations data passes through verbatim.

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import base.telemetry.alerts as base_alerts
from base.telemetry.alerts import (
    AlertMessageStyle,
    copy,
    format_local,
    frontend_base_url,
    notify_text,
)

# meiva:note
# What every message in this suite renders against, named once: the cluster zone the trigger time
# is shown in and the fleet-UI base URL the jump link points at. The renderers take these as
# values, so a case that wants a different zone or no jump link says so here instead of
# reconfiguring the process.
STYLE = AlertMessageStyle(zone=ZoneInfo("Asia/Shanghai"), base_url="http://fleet.test:3000")
NO_LINK = AlertMessageStyle(zone=ZoneInfo("Asia/Shanghai"), base_url="")


def _alert(*, status: str = "firing", severity: str = "error") -> dict[str, Any]:
    """An Alertmanager-webhook alert shape (the dict form notify_text reads)."""

    return {
        "status": status,
        "labels": {"alertname": "test-rule", "severity": severity},
        "annotations": {"summary": "test summary"},
        "starts_at": "2026-08-04T10:00:00Z",
        "generator_url": "http://localhost:3002/alerting/xyz/edit",
    }


def _head(*, resolved: bool, severity: str = "ERROR") -> str:
    return copy.ALERT_HEAD["resolved" if resolved else "firing"].format(
        severity=severity, alertname="test-rule"
    )


# -- template source (governance) -------------------------------------------


def test_alert_format_literals_not_hardcoded_in_alerts_module() -> None:
    """Governance (user ruling 2026-08-08): user-visible IM copy lives in
    base/telemetry/alerts/copy.py — the alert head literals must not creep back into
    base/telemetry/alerts/__init__.py."""
    src = Path(base_alerts.__file__).read_text(encoding="utf-8")
    for literal in (
        "⚠️ ALERT",  # emoji-ok: asserting the governance guard (head literals banned from base/telemetry/alerts/__init__.py)
        "✅ RESOLVED",  # emoji-ok: asserting the governance guard (head literals banned from base/telemetry/alerts/__init__.py)
    ):
        assert literal not in src


def test_notify_text_composes_from_copy_templates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """notify_text output is built from the alerts_copy templates — patching
    a template changes the message, so no format string is baked into the
    alerts module."""
    monkeypatch.setitem(base_alerts.ALERT_HEAD, "firing", "MARKER [{severity}] {alertname}")

    text = notify_text(_alert(), style=STYLE)

    assert text.startswith("MARKER [ERROR] test-rule")
    assert copy.ALERT_TRIGGERED_AT.split("{")[0] in text
    assert text.endswith(copy.ALERT_JUMP_LINK.format(url=STYLE.base_url))


# meiva:note
# The jump link points at the fleet UI entry: the HOST of the gateway URL (reachable over the
# private network) and the PORT of the frontend healthcheck URL — the app's own port is proxied,
# so a link built from it lands on the wrong listener. An unconfigured cluster yields no link.
def test_frontend_base_url_derives_the_host_from_the_gateway_and_the_port_from_the_ui() -> None:

    assert frontend_base_url("http://10.0.0.5:8080", "http://10.0.0.5:3000/health") == (
        "http://10.0.0.5:3000"
    ), (
        "the jump link must point at the fleet-UI ENTRY (the gateway host plus the UI's own "
        "port); the app's port is proxied, so a link built from MEIVA_GATEWAY_URL's port lands "
        "on the wrong listener"
    )
    assert frontend_base_url("", "") == "", (
        "an unconfigured cluster has no reachable fleet UI: the link is omitted, never "
        "replaced by a loopback or relative address the user cannot open (issue #134)"
    )


# meiva:note
# A cluster that configures no reachable fleet UI (the style's base URL is "") must not push a
# malformed jump-link line (issue #134): the line is omitted rather than rendered as a bare path.
def test_notify_text_omits_jump_link_when_no_frontend_base_url() -> None:

    text = notify_text(_alert(), style=NO_LINK)

    assert copy.ALERT_JUMP_LINK.split("{")[0] not in text
    assert "ALERT [ERROR] test-rule" in text
    # the rest of the message is intact — only the jump-link line is gone
    assert "triggered " in text
    assert text.count("\n") == 3


# -- English-only copy ---------------------------------------------------------


def test_notify_text_is_english_with_data_passed_through() -> None:
    """Firing and resolved heads are the English templates; data (summary,
    alertname, severity) passes through as received."""

    firing = notify_text(_alert(), style=STYLE)
    resolved = notify_text(_alert(status="resolved"), style=STYLE)

    assert firing.startswith(_head(resolved=False))
    assert resolved.startswith(_head(resolved=True))
    assert "triggered " in firing
    assert "test-rule" in firing and "test summary" in firing and "ERROR" in firing
    assert firing.endswith(copy.ALERT_JUMP_LINK.format(url=STYLE.base_url))


# -- format_local (tz audit, 2026-08, PR-6) ----------------------------------


def test_format_local_carries_year_and_zone() -> None:
    """The IM-push timestamp used to be bare `%m-%d %H:%M` — no year (an
    alert near New Year's read as an unstated year), no zone abbreviation (no
    way to tell which machine's local clock a multi-machine cluster's reader
    was looking at). Both must be present now."""

    ts = datetime.datetime(2026, 1, 3, 9, 5, tzinfo=datetime.UTC)
    text = format_local(ts, ZoneInfo("Asia/Shanghai"))
    assert text.startswith("2026-01-03 "), (
        "an alert near New Year's must carry its year: `%m-%d %H:%M` left the year unstated"
    )
    assert text.split(" ")[-1] != "", (
        "the zone token is the caller's, so the assertion is on the shape every zone renders"
    )


def test_format_local_empty_for_none() -> None:
    assert format_local(None, ZoneInfo("Asia/Shanghai")) == ""


def test_format_local_renders_cluster_zone() -> None:
    """User ruling 2026-08-27: the IM alert timestamp follows the cluster's
    MEIVA_TIMEZONE (one cluster clock), not the host OS zone — a runner whose
    OS zone differs must not show a different wall clock from every other
    surface."""

    ts = datetime.datetime(2026, 1, 3, 9, 5, tzinfo=datetime.UTC)
    assert format_local(ts, ZoneInfo("Asia/Shanghai")) == "2026-01-03 17:05 CST", (
        "the IM timestamp follows the cluster clock the root hands in (user ruling "
        "2026-08-27), not the runner's OS zone: two machines in one cluster must show the "
        "user the same wall clock"
    )


def test_format_local_host_zone_without_authoritative_value() -> None:
    """Settings-lite (gateway down) has no authoritative cluster timezone:
    the timestamp degrades to the host zone — the documented lite behavior —
    and must still carry year + a zone token."""

    ts = datetime.datetime(2026, 1, 3, 9, 5, tzinfo=datetime.UTC)
    text = format_local(ts, None)
    assert text.startswith("2026-01-03 "), (
        "the settings-lite degradation must still render a year and a zone token — a timestamp "
        "a reader cannot place is worse than one in the host zone"
    )
    assert text.split(" ")[-1] != ""


# -- grouped message ---------------------------------------------------------


def _instance(n: int, *, severity: str = "error", status: str = "firing") -> dict[str, Any]:
    alert = _alert(status=status, severity=severity)
    alert["annotations"] = {"summary": f"machine m{n} unreachable"}
    alert["starts_at"] = f"2026-08-04T10:0{n}:00Z"
    return alert


def test_a_lone_instance_keeps_the_single_alert_format() -> None:
    alert = _alert()
    assert base_alerts.notify_group_text([alert], style=STYLE) == notify_text(alert, style=STYLE)


def test_a_group_is_one_message_with_a_count_the_first_instances_and_the_rest_counted() -> None:
    alerts = [_instance(n, severity="warning" if n else "critical") for n in range(5)]
    text = base_alerts.notify_group_text(alerts, style=STYLE)
    lines = text.splitlines()
    assert lines[0] == _head(resolved=False, severity="CRITICAL") + " ×5"
    assert lines[1:4] == [f"- machine m{n} unreachable" for n in range(3)]
    assert lines[4] == copy.ALERT_GROUP_MORE.format(n=2)
    assert "m3" not in text and "m4" not in text
    assert any(line.startswith(copy.ALERT_TRIGGERED_AT.split("{")[0]) for line in lines)


def test_a_resolved_group_swaps_the_head_and_a_small_group_has_no_remainder_line() -> None:
    alerts = [_instance(n, status="resolved") for n in range(2)]
    text = base_alerts.notify_group_text(alerts, style=STYLE)
    assert text.splitlines()[0] == _head(resolved=True) + " ×2"
    assert copy.ALERT_GROUP_MORE.split("{")[0] not in text
