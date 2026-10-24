from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run_gate(
    user_home: Path, *, default: bool, jobs: str, refresh: str, tick: str
) -> subprocess.CompletedProcess[str]:
    home = user_home / (".meiva" if default else "scratch")
    env = {
        **os.environ,
        "HOME": str(user_home),
        "MEIVA_HOME": str(home),
        "MEIVA_CONFIG_FETCH": "skip",
        "MEIVA_OS_JOBS_ENABLED": jobs,
        "MEIVA_PACKAGES_REFRESH_ENABLED": refresh,
        "MEIVA_PACKAGES_REFRESH_TICK_SECONDS": tick,
    }
    env.pop("MEIVA_PROCESS_PROFILE", None)
    return subprocess.run(
        [sys.executable, "-m", "cli.commands.converge.tests.child_packages_job_gate"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize(
    ("default", "jobs", "refresh"),
    [(True, "false", "true"), (False, "true", "true"), (True, "true", "false")],
)
def test_earlier_gates_do_not_construct_the_unused_tick_policy(
    default_home: Path, default: bool, jobs: str, refresh: str
) -> None:
    result = _run_gate(
        default_home, default=default, jobs=jobs, refresh=refresh, tick="not-an-integer"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"upgrades": 0, "crontab": ""}


def test_an_active_owned_job_validates_the_tick_it_needs(default_home: Path) -> None:
    result = _run_gate(
        default_home, default=True, jobs="true", refresh="true", tick="not-an-integer"
    )
    assert result.returncode != 0
    assert "MEIVA_PACKAGES_REFRESH_TICK_SECONDS" in result.stderr


def test_the_root_registers_a_real_job_definition_with_its_configured_interval(
    default_home: Path,
) -> None:
    result = _run_gate(default_home, default=True, jobs="true", refresh="true", tick="3600")
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["upgrades"] == 1
    assert "*/60 * * * *" in receipt["crontab"]
    assert f"MEIVA_HOME={default_home / '.meiva'}" in receipt["crontab"]
    assert "packages refresh --from-job" in receipt["crontab"]


def test_a_malformed_channel_flag_still_fails_at_the_boot_boundary(default_home: Path) -> None:
    result = _run_gate(default_home, default=True, jobs="false", refresh="not-a-bool", tick="900")
    assert result.returncode != 0
    assert "MEIVA_PACKAGES_REFRESH_ENABLED" in result.stderr


def test_a_malformed_master_flag_still_fails_at_the_boot_boundary(default_home: Path) -> None:
    result = _run_gate(default_home, default=True, jobs="not-a-bool", refresh="true", tick="900")
    assert result.returncode != 0
    assert "MEIVA_OS_JOBS_ENABLED" in result.stderr
