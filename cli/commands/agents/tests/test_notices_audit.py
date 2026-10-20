from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from cli.commands.agents.notices_audit import cmd_notices_audit, render_json, render_text

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


# meiva:note
# A canned gateway: records the request and answers with one response.
class _Gateway:
    def __init__(self, response: Callable[[str], httpx.Response]) -> None:
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self._response = response

    def dial(self) -> tuple[str, dict[str, str], Callable[..., httpx.Response]]:
        def get(url: str, **kwargs: Any) -> httpx.Response:
            self.requests.append((url, kwargs["params"], kwargs["headers"]))
            return self._response(url)

        return "http://gw", {"Authorization": "Bearer t"}, get


def _ok(report: dict[str, Any]) -> Callable[[str], httpx.Response]:
    return lambda url: httpx.Response(200, json=report, request=httpx.Request("GET", url))


def test_the_command_asks_the_gateway_with_its_options_and_renders_the_report(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gateway = _Gateway(_ok(_REPORT))

    rc = cmd_notices_audit(
        days=3,
        category="core.needs_input",
        tz_name="Asia/Shanghai",
        now=datetime(2026, 10, 2, tzinfo=UTC),
        simulate=["requestion=off"],
        explain=True,
        dial=gateway.dial,
    )

    assert rc == 0
    assert gateway.requests == [
        (
            "http://gw/api/notifications/audit",
            {
                "days": 3,
                "explain": True,
                "simulate": ["requestion=off"],
                "category": "core.needs_input",
                "tz": "Asia/Shanghai",
                "now": "2026-10-02T00:00:00+00:00",
            },
            {"Authorization": "Bearer t"},
        )
    ]
    assert "Notification audit" in capsys.readouterr().out


def test_the_json_output_is_the_gateway_report(capsys: pytest.CaptureFixture[str]) -> None:
    gateway = _Gateway(_ok(_REPORT))

    rc = cmd_notices_audit(
        days=7, category=None, tz_name=None, now=None, simulate=[], out="json", dial=gateway.dial
    )

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == _REPORT
    assert gateway.requests[0][1] == {"days": 7, "explain": False, "simulate": []}


def test_a_rejected_request_prints_the_reason_and_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gateway = _Gateway(
        lambda url: httpx.Response(
            422,
            json={"detail": "category not declared by any plugin: x.y"},
            request=httpx.Request("GET", url),
        )
    )

    rc = cmd_notices_audit(
        days=7, category="x.y", tz_name=None, now=None, simulate=[], dial=gateway.dial
    )

    assert rc == 2
    assert "category not declared by any plugin: x.y" in capsys.readouterr().err


def test_a_gateway_error_is_not_swallowed() -> None:
    gateway = _Gateway(lambda url: httpx.Response(500, json={}, request=httpx.Request("GET", url)))

    with pytest.raises(httpx.HTTPStatusError):
        cmd_notices_audit(
            days=7, category=None, tz_name=None, now=None, simulate=[], dial=gateway.dial
        )
