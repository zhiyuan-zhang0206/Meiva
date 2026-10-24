# meiva:note
# Scheduler definitions are values, independent of host services and process roots.

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from base.host.system import logs_content, packages_content, pr_flow_content, walg_content
from base.host.system.job_values import JobPaths, JobRoots


def test_launchd_plist_runs_rotate_then_retention_daily(
    job_roots: JobRoots,
) -> None:
    content = logs_content.launchd_plist_content(
        paths=JobPaths(job_roots, "/work tree/.venv/bin/meiva"),
        path_env="/work tree/.venv/bin:/usr/bin",
    )
    # meiva:note
    # Only this test's rendered XML reaches the parser; no external XML is accepted.
    root = ET.fromstring(content)  # noqa: S314
    values = [element.text for element in root.findall("./dict/array/string")]
    command = values[2]

    assert "<string>com.meiva.logs-maintenance</string>" in content
    assert values[0:2] == ["/bin/sh", "-c"]
    assert command is not None
    assert "'/work tree/.venv/bin/meiva' logs rotate" in command
    assert "&& '/work tree/.venv/bin/meiva' logs retention" in command
    assert f"--family-days {logs_content.FAMILY_DAYS}" in command
    assert "<key>Hour</key>\n            <integer>4</integer>" in content
    assert "<key>Minute</key>\n            <integer>40</integer>" in content
    assert "<key>RunAtLoad</key>\n    <false/>" in content
    assert str(job_roots.home / "logs" / "logs-maintenance.out.log") in content


def test_launchd_plist_runs_the_refresh_pass_on_the_tick(
    job_roots: JobRoots, tmp_path: Path
) -> None:
    content = packages_content.launchd_plist_content(
        tick_seconds=900,
        paths=JobPaths(job_roots, "/work tree/.venv/bin/meiva"),
        path_env="/work tree/.venv/bin:/usr/bin",
    )
    # meiva:note
    # Only this test's rendered XML reaches the parser; no external XML is accepted.
    root = ET.fromstring(content)  # noqa: S314
    values = [element.text for element in root.findall("./dict/array/string")]
    command = values[2]

    assert "<string>com.meiva.packages-refresh</string>" in content
    assert values[0:2] == ["/bin/sh", "-c"]
    assert command is not None
    assert "'/work tree/.venv/bin/meiva' packages refresh --from-job" in command
    assert "<key>StartInterval</key>\n    <integer>900</integer>" in content
    assert "<key>RunAtLoad</key>\n    <false/>" in content
    assert f"{tmp_path}/.meiva/logs/packages-refresh.log" in content


def test_launchd_plist_schedules_the_daily_sampler(job_roots: JobRoots, tmp_path: Path) -> None:
    content = pr_flow_content.launchd_plist_content(
        paths=JobPaths(job_roots, "/work tree/.venv/bin/meiva"),
        path_env="/work tree/.venv/bin:/usr/bin",
    )
    # meiva:note
    # Only this test's rendered XML reaches the parser; no external XML is accepted.
    root = ET.fromstring(content)  # noqa: S314
    values = [element.text for element in root.findall("./dict/array/string")]

    assert "<string>com.meiva.pr-flow</string>" in content
    assert values[0:2] == ["/bin/sh", "-c"]
    command = values[2]
    assert command is not None
    assert "'/work tree/.venv/bin/python'" in command
    assert "/check/out/scripts/ci/pull_requests/pr_flow_export.py" in command
    assert (
        "<key>StartCalendarInterval</key>\n    <dict>\n"
        "            <key>Hour</key>\n            <integer>0</integer>\n"
        "            <key>Minute</key>\n            <integer>25</integer>\n"
    ) in content
    assert "<key>RunAtLoad</key>\n    <false/>" in content
    assert f"{tmp_path}/.meiva/logs/pr-flow.out.log" in content
    assert f"<string>{tmp_path}/.meiva</string>" in content


def test_the_tick_runs_three_hours_after_the_logical_dump_becomes_due() -> None:
    assert (walg_content.hour(backup_hour=3), walg_content.MINUTE) == (
        6,
        25,
    )


@pytest.mark.parametrize(("dump_hour", "tick_hour"), [(0, 3), (20, 23), (21, 0), (23, 2)])
def test_the_tick_hour_follows_the_logical_dump_hour_and_wraps(
    dump_hour: int, tick_hour: int
) -> None:
    assert walg_content.hour(backup_hour=dump_hour) == tick_hour


def test_launchd_plist_runs_the_tick_command_daily_and_logs_to_walg_log(
    job_roots: JobRoots, tmp_path: Path
) -> None:
    content = walg_content.launchd_plist_content(
        backup_hour=3,
        paths=JobPaths(job_roots, "/work tree/.venv/bin/meiva"),
        path_env="/work tree/.venv/bin:/usr/bin",
    )
    # meiva:note
    # Only this test's rendered XML reaches the parser; no external XML is accepted.
    root = ET.fromstring(content)  # noqa: S314
    values = [element.text for element in root.findall("./dict/array/string")]

    assert "<string>com.meiva.walg</string>" in content
    assert values == ["/bin/sh", "-c", "'/work tree/.venv/bin/meiva' backup walg run"]
    assert (
        "<key>StartCalendarInterval</key>\n    <dict>\n"
        "            <key>Hour</key>\n            <integer>6</integer>\n"
        "            <key>Minute</key>\n            <integer>25</integer>\n"
    ) in content
    assert "<key>RunAtLoad</key>\n    <false/>" in content
    assert content.count(f"{tmp_path}/.meiva/logs/walg.log") == 2
    assert f"<string>{tmp_path}/.meiva</string>" in content
