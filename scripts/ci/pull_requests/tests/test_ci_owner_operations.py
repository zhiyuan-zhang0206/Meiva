from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.host.net.ports import UrlOpener
from base.host.system.hostport import Host
from scripts.ci.pull_requests import monitor, status
from scripts.ci.pull_requests.tests.ci_evidence import check, monitor_host, probe, rollup


def _make_clock() -> Clock:
    timers = ManualTimers(wall_start=datetime(2026, 9, 13, 12, 0, tzinfo=UTC))
    return Clock(
        ClockConfig(
            timezone="UTC",
            authoritative_timezone="UTC",
            message_timestamp_weekday=False,
        ),
        timers=timers,
        now=timers.now,
    )


def _gh_runner(calls: list[list[str]]) -> Host:
    def run(argv: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    return monitor_host(probe(status.CIStatus.ALL_PASSED), calls=calls, other=run)


class _TrunkResponse:
    def __init__(self, payload: dict[str, object], *, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> _TrunkResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def _urlopen_sequence(
    responses: list[_TrunkResponse | urllib.error.URLError],
    seen_requests: list[urllib.request.Request],
) -> UrlOpener:
    response_iter = iter(responses)

    def urlopen(request: str | urllib.request.Request, timeout: float = 30.0) -> _TrunkResponse:
        del timeout
        assert isinstance(request, urllib.request.Request)
        seen_requests.append(request)
        response = next(response_iter)
        if isinstance(response, urllib.error.URLError):
            raise response
        return response

    return urlopen


def test_real_trunk_queue_checks_do_not_block_all_green_predicate() -> None:
    result = status.CIResult(verdict=status.CIStatus.ALL_PASSED)

    status.partition_checks(
        [
            {"name": "Trunk Merge Queue (main)", "status": "IN_PROGRESS", "conclusion": ""},
            {
                "name": "Trunk Merge Queue (main)",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
            },
        ],
        result,
    )

    assert result.pending == []
    assert result.failed == []
    assert result.trunk_checks == [
        {"name": "Trunk Merge Queue (main)", "status": "IN_PROGRESS", "conclusion": ""},
        {
            "name": "Trunk Merge Queue (main)",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
        },
    ]


def test_json_query_includes_trunk_checks(capsys: pytest.CaptureFixture[str]) -> None:
    queue_check = {"name": "Trunk Merge Queue (main)", "status": "IN_PROGRESS"}

    assert (
        monitor.query_once(
            "42",
            "zhiyuan-zhang0206/Meiva",
            as_json=True,
            host=monitor_host(rollup([check("test", "SUCCESS"), queue_check])),
            clock=_make_clock(),
            workflows=True,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["trunk_checks"] == [queue_check]


def test_query_once_not_ready_is_not_green(capsys: pytest.CaptureFixture[str]) -> None:

    assert (
        monitor.query_once(
            "42",
            "owner/repo",
            as_json=False,
            host=monitor_host(probe(status.CIStatus.NOT_READY)),
            clock=_make_clock(),
            workflows=True,
        )
        == 1
    )
    printed = capsys.readouterr().out
    assert "NOT READY" in printed and "draft gating" in printed


def test_json_query_includes_draft_fields(capsys: pytest.CaptureFixture[str]) -> None:

    assert (
        monitor.query_once(
            "42",
            "owner/repo",
            as_json=True,
            host=monitor_host(probe(status.CIStatus.NOT_READY)),
            clock=_make_clock(),
            workflows=True,
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["is_draft"] is True
    assert payload["core_skipped"] == ["backend shard (1/16)"]


def _trunk_runner(
    *,
    base_sha: str | None,
    main_sha: str | None,
    calls: list[list[str]],
) -> Host:
    """Stand in for gh/git subprocess calls: baseRefOid and ls-remote."""

    def run(argv: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        calls.append(argv)
        if "ls-remote" in argv:
            if main_sha is None:
                return subprocess.CompletedProcess(argv, 1, "", "boom")
            return subprocess.CompletedProcess(argv, 0, f"{main_sha}\trefs/heads/main\n", "")
        if "baseRefOid" in argv:
            if base_sha is None:
                return subprocess.CompletedProcess(argv, 1, "", "boom")
            return subprocess.CompletedProcess(argv, 0, f"{base_sha}\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "unmatched")

    return monitor_host(probe(status.CIStatus.ALL_PASSED), other=run)
