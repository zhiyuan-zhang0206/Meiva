from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest

from cli.commands.agents.notices_audit import cmd_notices_audit, render_json, render_text
from cli.commands.cluster.tests.gateway_server import Reply, access_to, serving

_LAST_DAY = (datetime(2026, 10, 2, tzinfo=UTC) - timedelta(days=1)).date().isoformat()

_REPORT: dict[str, Any] = {
    "window": {
        "start": "2026-09-30T00:00:00+00:00",
        "end": "2026-10-02T00:00:00+00:00",
        "days": 2,
        "timezone": "Asia/Shanghai",
    },
    "switches": {"core.needs_input": True},
    "categories": ["core.needs_input"],
    "sources": {"core.needs_input": "agent_notices (require_response post and edit)"},
    "delivered": {
        "total": 1,
        "per_day": [
            {
                "day": _LAST_DAY,
                "by_category": {"core.needs_input": 1},
                "total": 1,
            },
        ],
        "per_category": {"core.needs_input": 1},
        "per_hour": [1 if hour == 20 else 0 for hour in range(24)],
    },
    "candidates": [
        {
            "category": "core.needs_input",
            "candidates": 2,
            "delivered": 1,
            "suppressed": 1,
            "repeat_deliveries": 0,
        }
    ],
    "simulations": [{"variant": "baseline", "delivered": 1, "delta": 0, "per_day": 0.5}],
    "explain": None,
}

_EXPLAINED = {
    **_REPORT,
    "explain": [
        {
            "at": "2026-10-01T20:00:00+08:00",
            "category": "core.needs_input",
            "agent_id": 7,
            "dedupe_key": "notice:1",
            "outcome": "delivered",
        },
        {
            "at": "2026-10-01T20:30:00+08:00",
            "category": "core.needs_input",
            "agent_id": 7,
            "dedupe_key": "notice:1",
            "outcome": "duplicate 1800s",
        },
    ],
}


def test_the_text_report_names_every_section() -> None:
    text = "\n".join(render_text(_REPORT, tz=ZoneInfo("Asia/Shanghai")))

    for section in ("Deliveries per day", "Per category", "Candidates and suppression"):
        assert section in text
    assert "Simulations" in text
    assert "Why each candidate" not in text


def test_the_text_report_lists_the_explanation_when_the_gateway_sent_one() -> None:
    text = "\n".join(render_text(_EXPLAINED, tz=ZoneInfo("Asia/Shanghai")))

    assert "Why each candidate was or was not delivered" in text
    assert "duplicate 1800s" in text


def test_json_is_the_gateway_report_verbatim() -> None:
    assert json.loads(render_json(_REPORT)) == _REPORT


AUDIT = ("GET", "/api/notifications/audit")


def test_the_command_asks_the_gateway_with_its_options_and_renders_the_report(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serving({AUDIT: Reply(body=_REPORT)}) as gateway:
        rc = cmd_notices_audit(
            days=3,
            category="core.needs_input",
            tz_name="Asia/Shanghai",
            now=datetime(2026, 10, 2, tzinfo=UTC),
            simulate=["requestion=off"],
            explain=True,
            access=access_to(gateway.url),
        )

    assert rc == 0
    [request] = gateway.requests
    assert parse_qs(urlsplit(request.target).query) == {
        "days": ["3"],
        "explain": ["true"],
        "simulate": ["requestion=off"],
        "category": ["core.needs_input"],
        "tz": ["Asia/Shanghai"],
        "now": ["2026-10-02T00:00:00+00:00"],
    }
    assert request.headers["authorization"] == "Bearer test-cluster-secret"
    assert "Notification audit" in capsys.readouterr().out


def test_the_json_output_is_the_gateway_report(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({AUDIT: Reply(body=_REPORT)}) as gateway:
        rc = cmd_notices_audit(
            days=7,
            category=None,
            tz_name=None,
            now=None,
            simulate=[],
            out="json",
            access=access_to(gateway.url),
        )

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == _REPORT
    assert parse_qs(urlsplit(gateway.requests[0].target).query) == {
        "days": ["7"],
        "explain": ["false"],
    }


def test_a_rejected_request_prints_the_reason_and_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rejection = Reply(422, {"detail": "category not declared by any plugin: x.y"})
    with serving({AUDIT: rejection}) as gateway:
        rc = cmd_notices_audit(
            days=7,
            category="x.y",
            tz_name=None,
            now=None,
            simulate=[],
            access=access_to(gateway.url),
        )

    assert rc == 2
    assert "category not declared by any plugin: x.y" in capsys.readouterr().err


def test_a_gateway_error_is_not_swallowed() -> None:
    with serving({AUDIT: Reply(500, {})}) as gateway, pytest.raises(httpx.HTTPStatusError):
        cmd_notices_audit(
            days=7,
            category=None,
            tz_name=None,
            now=None,
            simulate=[],
            access=access_to(gateway.url),
        )
