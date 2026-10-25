# meiva:note
# Parse the actual pure definition; values are independent of process home and resources.

import json
import plistlib
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded
from base.host.system.cron import health_content


def test_definition_carries_the_observation_command_and_schedule(tmp_path: Path) -> None:
    xml = health_content.launchd_plist_content(
        arguments=("/private/meiva", "cluster", "health-probe"),
        interval_seconds=37,
        log_file=tmp_path / "log-home/logs/health-probe.log",
        environment_home=tmp_path / "environment-home",
        path_env="/private/bin:/usr/bin",
    )
    values = plistlib.loads(xml.encode())
    assert values["Label"] == "com.meiva.health-probe"
    assert values["ProgramArguments"] == ["/private/meiva", "cluster", "health-probe"]
    assert values["StartInterval"] == 37
    assert values["RunAtLoad"] is False
    assert "KeepAlive" not in values
    assert values["EnvironmentVariables"] == {
        "MEIVA_HOME": str(tmp_path / "environment-home"),
        "PATH": "/private/bin:/usr/bin",
    }
    assert values["StandardOutPath"] == str(tmp_path / "log-home/logs/health-probe.log")
    assert values["StandardErrorPath"] == values["StandardOutPath"]


@pytest.mark.parametrize("field", ["binary", "log"])
def test_xml_metacharacters_round_trip_in_each_affected_field(tmp_path: Path, field: str) -> None:
    binary = "/private/bin & <entry>/meiva" if field == "binary" else "/safe/meiva"
    log_file = tmp_path / ("log & <output>.log" if field == "log" else "safe.log")
    environment_home = tmp_path / "independent & <environment>"
    values = plistlib.loads(
        health_content.launchd_plist_content(
            arguments=(binary, "cluster", "health-probe"),
            interval_seconds=300,
            log_file=log_file,
            environment_home=environment_home,
            path_env="/safe & <path>:/usr/bin",
        ).encode()
    )
    assert values["ProgramArguments"] == [binary, "cluster", "health-probe"]
    assert values["StandardOutPath"] == str(log_file)
    assert values["StandardErrorPath"] == str(log_file)
    assert values["EnvironmentVariables"] == {
        "MEIVA_HOME": str(environment_home),
        "PATH": "/safe & <path>:/usr/bin",
    }


def test_planned_argv_and_home_reach_a_short_real_recorder(tmp_path: Path) -> None:
    recorder = Path(__file__).with_name("child_definition.py")
    home = tmp_path / "private & <home>"
    values = plistlib.loads(
        health_content.launchd_plist_content(
            arguments=(sys.executable, "-I", str(recorder), "record", "cluster", "health-probe"),
            interval_seconds=300,
            log_file=tmp_path / "different-log-home/out.log",
            environment_home=home,
            path_env="/usr/bin:/bin",
        ).encode()
    )
    result = run_bounded(
        values["ProgramArguments"],
        env=values["EnvironmentVariables"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"argv": ["cluster", "health-probe"], "home": str(home)}
