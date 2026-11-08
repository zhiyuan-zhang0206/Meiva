# meiva:note
# Execute generated commands in a disposable filesystem, without a host scheduler.

import json
import os
import plistlib
import shlex
import sys
from datetime import date
from pathlib import Path

import pytest

from base.host.proc import run_bounded
from base.host.system import logs_content, packages_content, pr_flow_content, walg_content
from base.host.system.cron import heartbeat_population
from base.host.system.job_values import HeartbeatRender, JobPaths, JobRoots, JobSchedule

_TODAY = date(2026, 11, 8)

# meiva:note
# The executable records the actual shell-delivered argv/environment. It never starts
# Meiva or contacts a service; only the four production renderers decide the command.
_RECORDER = """import json, os, sys
with open(os.environ['CAPTURE'], 'a') as output:
    output.write(json.dumps({'argv': sys.argv[1:], 'home': os.environ['MEIVA_HOME']}) + '\\n')
if sys.argv[1:] == ['logs', 'maintenance']:
    sys.exit(int(os.environ.get('ROTATE_RC', '0')))
"""


def _paths(tmp_path: Path) -> JobPaths:
    repo = tmp_path / "checkout & <repo> 'quoted'"
    home = tmp_path / "cluster & <home> 'quoted'"
    binaries = repo / ".venv" / "bin"
    binaries.mkdir(parents=True)
    (home / "logs").mkdir(parents=True)
    recorder = repo / "record.py"
    recorder.write_text(_RECORDER, encoding="utf-8")
    meiva = binaries / "meiva"
    meiva.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(recorder))} "$@"\n',
        encoding="utf-8",
    )
    meiva.chmod(0o700)
    (binaries / "python").symlink_to(sys.executable)
    script = repo / "scripts" / "ci" / "pull_requests" / "pr_flow_export.py"
    script.parent.mkdir(parents=True)
    script.write_text(_RECORDER, encoding="utf-8")
    return JobPaths(JobRoots(home=home, repo=repo, user_home=tmp_path), str(meiva))


def _heartbeat(label: str, paths: JobPaths, schedule: JobSchedule) -> HeartbeatRender:
    return heartbeat_population.verified(
        label, roots=paths.roots, machine="macmini", uid=501, schedule=schedule, today=_TODAY
    )


def _definitions(kind: str, paths: JobPaths) -> tuple[str, str]:
    path_env = "/usr/bin:/bin:/a & <path>"
    match kind:
        case "logs":
            heartbeat = _heartbeat(logs_content.LABEL, paths, logs_content.schedule())
            return (
                logs_content.launchd_plist_content(
                    paths=paths, path_env=path_env, heartbeat=heartbeat
                ),
                logs_content.cron_entry(paths, heartbeat=heartbeat),
            )
        case "packages":
            heartbeat = _heartbeat(
                packages_content.LABEL, paths, packages_content.schedule(tick_seconds=900)
            )
            return (
                packages_content.launchd_plist_content(
                    paths=paths, path_env=path_env, tick_seconds=900, heartbeat=heartbeat
                ),
                packages_content.cron_entry(paths, tick_seconds=900, heartbeat=heartbeat),
            )
        case "pr-flow":
            heartbeat = _heartbeat(pr_flow_content.LABEL, paths, pr_flow_content.schedule())
            return (
                pr_flow_content.launchd_plist_content(
                    paths=paths, path_env=path_env, heartbeat=heartbeat
                ),
                pr_flow_content.cron_entry(paths, heartbeat=heartbeat),
            )
        case "walg":
            heartbeat = _heartbeat(walg_content.LABEL, paths, walg_content.schedule(backup_hour=23))
            return (
                walg_content.launchd_plist_content(
                    paths=paths, path_env=path_env, backup_hour=23, heartbeat=heartbeat
                ),
                walg_content.cron_entry(paths, backup_hour=23, heartbeat=heartbeat),
            )
        case _:
            raise ValueError(kind)


@pytest.mark.parametrize("kind", ["logs", "packages", "pr-flow", "walg"])
@pytest.mark.parametrize("scheduler", ["launchd", "cron"])
def test_generated_job_delivers_real_argv_and_environment(
    tmp_path: Path, kind: str, scheduler: str
) -> None:
    paths = _paths(tmp_path)
    body, entry = _definitions(kind, paths)
    plist = plistlib.loads(body.encode())
    heartbeat_path = plist["EnvironmentVariables"]["MEIVA_JOB_HEARTBEAT"]
    assert Path(heartbeat_path).parent == paths.roots.home / "state" / "heartbeats"
    assert plist["EnvironmentVariables"] == {
        "MEIVA_HOME": str(paths.roots.home),
        "PATH": "/usr/bin:/bin:/a & <path>",
        "MEIVA_JOB_HEARTBEAT": heartbeat_path,
    }
    assert Path(plist["StandardOutPath"]).parent == paths.roots.home / "logs"
    assert plist["StandardErrorPath"] == plist["StandardOutPath"]
    capture = tmp_path / "delivery.jsonl"
    env = {**os.environ, "CAPTURE": str(capture)}
    if scheduler == "launchd":
        env.update(plist["EnvironmentVariables"])
        argv = plist["ProgramArguments"]
    else:
        argv = ["/bin/sh", "-c", entry.split(" ", 5)[5]]
    result = run_bounded(argv, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert (result.returncode, result.stderr) == (0, "")
    records = [json.loads(line) for line in capture.read_text().splitlines()]
    expected: dict[str, list[list[str]]] = {
        "logs": [["logs", "maintenance"]],
        "packages": [["packages", "refresh", "--from-job"]],
        "pr-flow": [[]],
        "walg": [["backup", "walg", "run"]],
    }
    assert records == [{"argv": args, "home": str(paths.roots.home)} for args in expected[kind]]


# meiva:note
# The job must fail when maintenance fails.
#
# This is the whole visibility story for a version skew: a CLI too old to know
# `logs maintenance` exits non-zero, and the only place that becomes observable is
# launchd's `last exit code`. A shell that swallowed the exit code would turn a broken
# log-maintenance job into silence - worse than the wrong retention window it replaced.
def test_the_job_reports_the_maintenance_exit_code(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    body, _ = _definitions("logs", paths)
    plist = plistlib.loads(body.encode())
    capture = tmp_path / "delivery.jsonl"
    result = run_bounded(
        plist["ProgramArguments"],
        cwd=tmp_path,
        env={
            **os.environ,
            **plist["EnvironmentVariables"],
            "CAPTURE": str(capture),
            "ROTATE_RC": "19",
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 19
    assert [json.loads(line)["argv"] for line in capture.read_text().splitlines()] == [
        ["logs", "maintenance"]
    ]
