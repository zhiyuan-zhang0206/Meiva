# meiva:note
# Execute generated commands in a disposable filesystem, without a host scheduler.

import json
import os
import plistlib
import shlex
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded
from base.host.system import logs_content, packages_content, pr_flow_content, walg_content
from base.host.system.job_values import JobPaths, JobRoots

# meiva:note
# The executable records the actual shell-delivered argv/environment. It never starts
# Meiva or contacts a service; only the four production renderers decide the command.
_RECORDER = """import json, os, sys
with open(os.environ['CAPTURE'], 'a') as output:
    output.write(json.dumps({'argv': sys.argv[1:], 'home': os.environ['MEIVA_HOME']}) + '\\n')
if sys.argv[1:] == ['logs', 'rotate']:
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


def _definitions(kind: str, paths: JobPaths) -> tuple[str, str]:
    path_env = "/usr/bin:/bin:/a & <path>"
    match kind:
        case "logs":
            return (
                logs_content.launchd_plist_content(paths=paths, path_env=path_env),
                logs_content.cron_entry(paths),
            )
        case "packages":
            return (
                packages_content.launchd_plist_content(
                    paths=paths, path_env=path_env, tick_seconds=900
                ),
                packages_content.cron_entry(paths, tick_seconds=900),
            )
        case "pr-flow":
            return (
                pr_flow_content.launchd_plist_content(paths=paths, path_env=path_env),
                pr_flow_content.cron_entry(paths),
            )
        case "walg":
            return (
                walg_content.launchd_plist_content(paths=paths, path_env=path_env, backup_hour=23),
                walg_content.cron_entry(paths, backup_hour=23),
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
    assert plist["EnvironmentVariables"] == {
        "MEIVA_HOME": str(paths.roots.home),
        "PATH": "/usr/bin:/bin:/a & <path>",
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
    expected = {
        "logs": [
            ["logs", "rotate"],
            ["logs", "retention", "--family-days", logs_content.FAMILY_DAYS],
        ],
        "packages": [["packages", "refresh", "--from-job"]],
        "pr-flow": [[]],
        "walg": [["backup", "walg", "run"]],
    }
    assert records == [{"argv": args, "home": str(paths.roots.home)} for args in expected[kind]]


def test_rotate_failure_stops_retention_in_the_real_shell(tmp_path: Path) -> None:
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
        ["logs", "rotate"]
    ]
