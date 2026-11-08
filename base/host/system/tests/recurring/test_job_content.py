# meiva:note
# Scheduler definitions are values, independent of host services and process roots.

import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest

from base.host.system import logs_content, packages_content, pr_flow_content, walg_content
from base.host.system.cron import heartbeat_population
from base.host.system.job_values import HeartbeatRender, JobPaths, JobRoots, JobSchedule

_TODAY = date(2026, 11, 8)


# meiva:note
# The product a registrar hands a renderer: the job's own declaration, validated against the
# whole population on a fixed day.
def heartbeat_for(
    label: str, roots: JobRoots, schedule: JobSchedule, *, today: date = _TODAY
) -> HeartbeatRender:
    return heartbeat_population.verified(
        label, roots=roots, machine="macmini", uid=501, schedule=schedule, today=today
    )


def test_launchd_plist_runs_maintenance_daily(
    job_roots: JobRoots,
) -> None:
    paths = JobPaths(job_roots, "/work tree/.venv/bin/meiva")
    content = logs_content.launchd_plist_content(
        paths=paths,
        path_env="/work tree/.venv/bin:/usr/bin",
        heartbeat=heartbeat_for(logs_content.LABEL, job_roots, logs_content.schedule()),
    )
    # meiva:note
    # Only this test's rendered XML reaches the parser; no external XML is accepted.
    root = ET.fromstring(content)  # noqa: S314
    values = [element.text for element in root.findall("./dict/array/string")]
    command = values[2]

    assert "<string>com.meiva.logs-maintenance</string>" in content
    assert values[0:2] == ["/bin/sh", "-c"]
    assert command is not None
    assert command == "'/work tree/.venv/bin/meiva' logs maintenance"
    # meiva:note
    # The job command must carry no policy. A generated plist is rewritten by whichever
    # install runs converge, so a tier list baked here is a copy that any release can
    # overwrite - on 2026-11-05 an update restored the older tiers eight minutes after a
    # seven-day policy was applied. The retention window now lives in
    # MEIVA_LOG_FAMILY_DAYS and is read when the command runs.
    assert "--family-days" not in command
    assert "agent=7" not in command
    assert "<key>Hour</key>\n            <integer>4</integer>" in content
    assert "<key>Minute</key>\n            <integer>40</integer>" in content
    assert "<key>RunAtLoad</key>\n    <false/>" in content
    assert str(job_roots.home / "logs" / "logs-maintenance.out.log") in content


def test_launchd_plist_runs_the_refresh_pass_on_the_tick(
    job_roots: JobRoots, tmp_path: Path
) -> None:
    paths = JobPaths(job_roots, "/work tree/.venv/bin/meiva")
    content = packages_content.launchd_plist_content(
        tick_seconds=900,
        paths=paths,
        path_env="/work tree/.venv/bin:/usr/bin",
        heartbeat=heartbeat_for(
            packages_content.LABEL, job_roots, packages_content.schedule(tick_seconds=900)
        ),
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
    paths = JobPaths(job_roots, "/work tree/.venv/bin/meiva")
    content = pr_flow_content.launchd_plist_content(
        paths=paths,
        path_env="/work tree/.venv/bin:/usr/bin",
        heartbeat=heartbeat_for(pr_flow_content.LABEL, job_roots, pr_flow_content.schedule()),
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
    paths = JobPaths(job_roots, "/work tree/.venv/bin/meiva")
    content = walg_content.launchd_plist_content(
        backup_hour=3,
        paths=paths,
        path_env="/work tree/.venv/bin:/usr/bin",
        heartbeat=heartbeat_for(
            walg_content.LABEL, job_roots, walg_content.schedule(backup_hour=3)
        ),
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
