# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownMemberType = warning
"""Ci monitor cases: runs probe reads only incomplete runs."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from typing import Any

import pytest

from base.host.net.tests.fake_url_opener import FakeUrlOpener
from base.host.system.tests.fake_commands import fake_host
from scripts.ci.pull_requests import commands as ci_utils
from scripts.ci.pull_requests import status
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    APP_CHECK,
    TEST_CLOCK,
    CIStatus,
    core_rollup,
    status_check_rollup_entry,
)
from scripts.ci.pull_requests.tests.test_ci_monitor import diag_gh as diag_gh
from scripts.ci.pull_requests.tests.test_ci_monitor import gh as gh
from scripts.ci.pull_requests.tests.test_ci_monitor import no_sleep as no_sleep
from scripts.ci.pull_requests.tests.test_ci_monitor import poll as poll

_RN = status.DEFAULT_REPO[18:]


def test_runs_probe_reads_only_incomplete_runs() -> None:
    """The jq the probe sends filters on `status != completed` — a finished run
    that produced no check is not evidence of one still coming."""
    seen: dict[str, list[str]] = {}

    def _run(argv: Sequence[str], *_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        seen["cmd"] = argv
        return subprocess.CompletedProcess(argv, 0, '["CI"]', "")

    host = fake_host(_run)
    assert status.runs_not_yet_reporting("abc123", None, host=host) == ["CI"]
    joined = " ".join(seen["cmd"])
    assert "head_sha=abc123" in joined
    assert 'select(.status != "completed")' in joined


@pytest.mark.parametrize("returncode, stdout", [(1, ""), (0, "not json")])
def test_runs_probe_returns_none_on_bad_response(returncode: int, stdout: str) -> None:
    """A failed query is None (unanswerable), not [] (nothing scheduled) — the
    distinction is what keeps an unanswerable probe from reading as green."""
    response = subprocess.CompletedProcess([], returncode, stdout, "boom")
    host = fake_host(lambda *_a, **_k: response)
    assert status.runs_not_yet_reporting("abc123", None, host=host) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-24T22:36:05Z", 1787610965.0),
        ('"2026-08-24T22:36:05Z"', 1787610965.0),
        ("null", None),
        ("bad", None),
    ],
)
def test_parse_ts(value: str | None, expected: float | None) -> None:
    assert status.parse_ts(value) == expected


def test_wait_all_passed_exits_zero(no_sleep, poll, capsys) -> None:
    host = poll(CIStatus.ALL_PASSED)
    assert (
        ci_utils.main(
            ["1243", "--wait"],
            clock=no_sleep,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 0
    )
    assert "CI green" in capsys.readouterr().out


@pytest.mark.parametrize(
    "verdict",
    [CIStatus.FAILED, CIStatus.MERGE_CONFLICT, CIStatus.NO_WORKFLOW_RUNS, CIStatus.NOT_READY],
)
def test_wait_not_green_exits_one(no_sleep, poll, capsys, verdict) -> None:
    host = poll(verdict)
    assert (
        ci_utils.main(
            ["1243", "--wait"],
            clock=no_sleep,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 1
    )
    assert "NOT green" in capsys.readouterr().err


def test_wait_not_ready_names_draft_gating(no_sleep, poll, capsys) -> None:
    host = poll(CIStatus.NOT_READY)
    assert (
        ci_utils.main(
            ["1243", "--wait"],
            clock=no_sleep,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 1
    )
    assert "draft gating" in capsys.readouterr().err


def test_wait_failed_lists_failed_checks(no_sleep, poll, capsys) -> None:
    host = poll(CIStatus.FAILED)
    assert (
        ci_utils.main(
            ["1243", "--wait"],
            clock=no_sleep,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 1
    )
    assert "lint" in capsys.readouterr().err


def test_wait_pending_then_green(no_sleep, poll) -> None:
    host = poll(CIStatus.PENDING, CIStatus.PENDING, CIStatus.ALL_PASSED)
    assert (
        ci_utils.main(
            ["1243", "--wait"],
            clock=no_sleep,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 0
    )


def test_wait_no_checks_then_green(no_sleep, poll, capsys) -> None:
    host = poll(CIStatus.NO_CHECKS, CIStatus.ALL_PASSED)
    assert (
        ci_utils.main(
            ["1243", "--wait"],
            clock=no_sleep,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 0
    )
    assert "no checks yet" in capsys.readouterr().err


def test_wait_transient_error_then_green(no_sleep, poll) -> None:
    # One gh/network hiccup must not kill the poll — only 3 consecutive do.
    host = poll(CIStatus.ERROR, CIStatus.PENDING, CIStatus.ALL_PASSED)
    assert (
        ci_utils.main(
            ["1243", "--wait"],
            clock=no_sleep,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 0
    )


def test_wait_three_consecutive_errors_exit_three(no_sleep, poll, capsys) -> None:
    host = poll(CIStatus.ERROR)
    assert (
        ci_utils.main(
            ["1243", "--wait"],
            clock=no_sleep,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 3
    )
    assert "boom" in capsys.readouterr().err


def test_wait_timeout_while_pending_exits_one(no_sleep, poll, capsys) -> None:
    # --timeout bounds the wait for run_background use (no watchdog there);
    # a still-pending PR at the deadline is a failed watch, not a silent hang.
    host = poll(CIStatus.PENDING)
    assert (
        ci_utils.main(
            ["1243", "--wait", "--timeout", "1"],
            clock=no_sleep,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 1
    )
    assert "timed out" in capsys.readouterr().err


def test_wait_usage_errors_exit_two() -> None:
    with pytest.raises(SystemExit) as e:
        ci_utils.main(
            [], clock=TEST_CLOCK, host=fake_host(), open_url=FakeUrlOpener(), workflows=True, env={}
        )
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        ci_utils.main(
            ["1243", "--wait", "--every", "0"],
            clock=TEST_CLOCK,
            host=fake_host(),
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        ci_utils.main(
            ["1243", "--wait", "--timeout", "-5"],
            clock=TEST_CLOCK,
            host=fake_host(),
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
    assert e.value.code == 2


def test_wait_json_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as e:
        ci_utils.main(
            ["1243", "--wait", "--json"],
            clock=TEST_CLOCK,
            host=fake_host(),
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
    assert e.value.code == 2


def test_one_shot_behavior_unchanged(gh: Any, capsys) -> None:
    """Legacy contract: without --wait, a PENDING probe prints and exits 0 —
    a one-shot query, not a poller."""
    host = gh([APP_CHECK, status_check_rollup_entry("backend", "", status="IN_PROGRESS")])
    assert (
        ci_utils.main(
            ["1243"], host=host, clock=TEST_CLOCK, open_url=FakeUrlOpener(), workflows=True, env={}
        )
        == 0
    )
    out = capsys.readouterr()
    assert "pending" in out.out.lower()


def test_one_shot_not_ready_exits_one(gh: Any, capsys) -> None:
    host = gh(core_rollup("SKIPPED"), is_draft=True, main_completed=True)
    assert (
        ci_utils.main(
            ["1243"], host=host, clock=TEST_CLOCK, open_url=FakeUrlOpener(), workflows=True, env={}
        )
        == 1
    )
    assert "NOT READY" in capsys.readouterr().out


@pytest.mark.parametrize(
    "verdict",
    [
        CIStatus.ALL_PASSED,
        CIStatus.FAILED,
        CIStatus.NOT_READY,
        CIStatus.MERGE_CONFLICT,
        CIStatus.NO_CHECKS,
        CIStatus.NO_WORKFLOW_RUNS,
        CIStatus.ERROR,
    ],
)
def test_settled_verdicts_are_terminal(verdict: CIStatus) -> None:
    assert verdict.is_terminal


def test_pending_is_not_terminal() -> None:
    assert not CIStatus.PENDING.is_terminal


def test_json_probe_carries_terminal_flag(gh: Any, capsys) -> None:
    """The --json one-shot output includes `terminal`, so a subprocess-based
    watcher can decide without re-deriving the verdict domain."""
    host = gh([APP_CHECK, status_check_rollup_entry("backend (pytest + pyright)", "SUCCESS")])
    assert (
        ci_utils.main(
            ["1243", "--json"],
            host=host,
            clock=TEST_CLOCK,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "all_passed"
    assert payload["terminal"] is True
    assert "gate_checks" not in payload


def test_json_probe_reports_draft_and_core_skipped(gh: Any, capsys) -> None:
    host = gh(core_rollup("SKIPPED"), is_draft=True, main_completed=True)
    assert (
        ci_utils.main(
            ["1243", "--json"],
            host=host,
            clock=TEST_CLOCK,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "not_ready"
    assert payload["terminal"] is True
    assert payload["is_draft"] is True
    assert payload["core_skipped"]


def test_json_probe_pending_is_not_terminal(gh: Any, capsys) -> None:
    host = gh([APP_CHECK, status_check_rollup_entry("backend", "", status="IN_PROGRESS")])
    assert (
        ci_utils.main(
            ["1243", "--json"],
            host=host,
            clock=TEST_CLOCK,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "pending"
    assert payload["terminal"] is False


def test_evict_requires_pr_number() -> None:
    with pytest.raises(SystemExit) as e:
        ci_utils.main(
            ["--evict"],
            clock=TEST_CLOCK,
            host=fake_host(),
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
    assert e.value.code == 2
