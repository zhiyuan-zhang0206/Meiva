# meiva:note
# `meiva jobs heartbeats` is the document a reader outside this checkout diffs the scheduler
# against, so the two properties that matter are the ones asserted here: the declared population
# arrives intact, and a machine whose declarations cannot be read exits non-zero instead of
# printing an empty set — "expects nothing" and "cannot tell" must not look alike.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from base.host.system.cron import heartbeat_population


def _run(home: Path, *, machine_name: str | None) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "MEIVA_HOME": str(home),
        "MEIVA_CONFIG_FETCH": "skip",
    }
    # meiva:note
    # The profile is a launcher's declaration that this process is an agent, a gateway or a
    # runner; an operator command is none of them and reads the configuration a converge reads.
    env.pop("MEIVA_PROCESS_PROFILE", None)
    if machine_name is not None:
        (home / ".env").write_text(
            f"MEIVA_MACHINE_NAME={machine_name}\n"
            "MEIVA_MACHINE_SERVE_GATEWAY=false\n"
            "MEIVA_MACHINE_SERVE_AGENT_RUNNER=false\n",
            encoding="utf-8",
        )
    return subprocess.run(
        [sys.executable, "-m", "cli.commands.jobs.tests.child_heartbeats"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    target = tmp_path / ".meiva"
    target.mkdir(parents=True)
    return target


def test_the_document_lists_every_declared_job(home: Path) -> None:
    result = _run(home, machine_name="host-a")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stderr.splitlines()[-1]) == {"rc": 0}

    document = json.loads(result.stdout)
    assert document["schema"] == 1
    assert document["machine"] == "host-a"
    assert len(document["jobs"]) == len(heartbeat_population.POPULATION) == 4
    names = {job["heartbeat_name"] for job in document["jobs"]}
    assert names == {"logs-maintenance", "packages-refresh", "pr-flow", "walg"}
    assert all(
        job["heartbeat_dir"] == str(home / "state" / "heartbeats") for job in document["jobs"]
    )


def test_a_machine_without_a_name_fails_instead_of_expecting_nothing(home: Path) -> None:
    result = _run(home, machine_name=None)
    assert result.returncode == 1
    assert "machine name not set" in result.stderr
    assert '"jobs"' not in result.stdout
