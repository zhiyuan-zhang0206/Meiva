from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from base.host.permissions_helper.grants import checklist, gate
from base.host.permissions_helper.grants.catalog import PING_KEYS, PREFLIGHT_SERVICES, Grant
from base.host.permissions_helper.grants.probe import ProbeResult, RequestGrantsResult
from cli.commands import permissions as cmd
from services.desktop.permissions_helper.onboarding import GateHost

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def _report(**overrides: Grant) -> gate.GateReport:
    grants = dict.fromkeys(PREFLIGHT_SERVICES, Grant.GRANTED)
    booleans = dict.fromkeys(PING_KEYS, True)
    for item_id, grant in overrides.items():
        item = gate.item_for(item_id)
        if item.ping_key:
            booleans[item.ping_key] = grant is Grant.GRANTED
        for service in item.services:
            grants[service] = grant
    return gate.evaluate(
        ProbeResult(grants=grants, booleans=booleans, supports_request=True),
        now=NOW,
    )


def _args(**kwargs: Any) -> argparse.Namespace:
    base = {"permissions_cmd": "status", "json": False, "timeout": 90.0}
    base.update(kwargs)
    return argparse.Namespace(**base)


class FakeHelper:
    def __init__(self, **overrides: Grant) -> None:
        self._codes = dict.fromkeys(PREFLIGHT_SERVICES, 0)
        self._booleans = dict.fromkeys(PING_KEYS, True)
        for item_id, grant in overrides.items():
            item = gate.item_for(item_id)
            if item.ping_key:
                self._booleans[item.ping_key] = grant is Grant.GRANTED
            code = {Grant.GRANTED: 0, Grant.DENIED: 1, Grant.NOT_DETERMINED: 2}[grant]
            for service in item.services:
                self._codes[service] = code
        self.requests = 0

    def ping(self) -> Mapping[str, object]:
        return {
            "pong": True,
            "request_grants_v1": True,
            **self._booleans,
        }

    def request_grants(self) -> RequestGrantsResult:
        self.requests += 1
        self._codes = dict.fromkeys(PREFLIGHT_SERVICES, 0)
        self._booleans = dict.fromkeys(PING_KEYS, True)
        return {
            "screen_recording": True,
            "accessibility": True,
        }

    def spawn_process(
        self,
        name: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        stdout: str,
        stderr: str,
    ) -> Mapping[str, object]:
        services = json.loads(argv[2])
        Path(argv[3]).write_text(
            json.dumps({service: {"code": self._codes[service]} for service in services})
        )
        return {"pid": 1, "reused": False}


def _host(tmp_path: Path, **overrides: Grant) -> tuple[GateHost, FakeHelper]:
    helper = FakeHelper(**overrides)
    seat = GateHost(
        client=helper,
        env={},
        workdir=tmp_path / "scratch",
        home=tmp_path,
        record_path=tmp_path / gate.RECORD_NAME,
        platform="darwin",
    )
    return seat, helper


def test_status_exits_nonzero_while_something_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seat, _ = _host(tmp_path, **{"screen-recording": Grant.DENIED})
    assert cmd.run(_args(), seat, now=NOW) == 1
    assert "Screen Recording" in capsys.readouterr().out


def test_status_exits_zero_on_a_complete_host(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seat, _ = _host(tmp_path)
    assert cmd.run(_args(), seat, now=NOW) == 0
    assert "Every required authorization is granted." in capsys.readouterr().out


def test_json_output_is_the_checklist_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report(**{"desktop-folder": Grant.NOT_DETERMINED})
    seat, _ = _host(tmp_path, **{"desktop-folder": Grant.NOT_DETERMINED})
    cmd.run(_args(json=True), seat, now=NOW)
    payload = json.loads(capsys.readouterr().out)
    assert payload == checklist.payload(report)
    assert payload["required_missing"] == 1


def test_the_cli_onboard_is_the_manual_pass(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seat, helper = _host(tmp_path, **{"screen-recording": Grant.DENIED})
    assert cmd.run(_args(permissions_cmd="onboard"), seat, now=NOW) == 0
    assert helper.requests == 1
    assert "Every required authorization is granted." in capsys.readouterr().out


def test_defer_says_plainly_that_nothing_was_granted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seat, _ = _host(tmp_path)
    assert cmd.run(_args(permissions_cmd="defer"), seat, now=NOW) == 0
    assert "Nothing is granted by this" in capsys.readouterr().out


def test_restart_helper_prints_the_operator_sentence(capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd.run(_args(permissions_cmd="restart-helper")) == 0
    assert "meiva stop" in capsys.readouterr().out
