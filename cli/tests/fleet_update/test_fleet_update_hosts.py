# meiva:note
# The per-host pieces a window builds and reads: the macOS one-shot start script and its plist,
# the updater's own import surface, and the PROBE program run through the host's real shell.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

import json
import os
import plistlib
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.native_process.root_control import instance_lock
from base.paths import repo_root
from cli.fleet_update import fleet_verify, oneshot
from cli.fleet_update.plan.host_commands import PROBE
from cli.fleet_update.session import Pacing, Session
from cli.fleet_update.timers import ProcessTimers
from cli.tests.fleet_update.fleet_update_cluster import git, ssh_on_path

_STAMP = "20261024T120000"


def test_macos_start_is_a_gui_one_shot() -> None:
    script = oneshot.gui_oneshot("mac", _STAMP, 60, "--all-services")
    job = plistlib.loads(
        script.split("<<'MEIVA_FLEET_PLIST'\n")[1].split("\nMEIVA_FLEET_PLIST")[0].encode()
    )
    assert 'launchctl bootstrap "gui/$(id -u)"' in script
    assert f'launchctl bootout "gui/$(id -u)/{job["Label"]}"' in script
    assert job["RunAtLoad"] is True and job["KeepAlive"] is False
    assert job["ProgramArguments"][:2] == ["/bin/zsh", "-lc"]
    assert 'MEIVA_HOME="$H" "$S/.venv/bin/meiva" start --all-services' in job["ProgramArguments"][2]
    assert shlex.split(f"zsh -lc {shlex.quote(script)}")[2] == script and 'rm -rf "$D"' in script


def test_the_macos_one_shot_start_passes_the_home_and_os_job_gates(default_home: Path) -> None:
    from base.host.system.cron import mechanics
    from base.host.system.job_values import JobRoots
    from cli.preflight import check_own_checkout

    script = oneshot.gui_oneshot("mac", _STAMP, 60, "--all-services")
    job = plistlib.loads(
        script.split("<<'MEIVA_FLEET_PLIST'\n")[1].split("\nMEIVA_FLEET_PLIST")[0].encode()
    )
    assert job["Label"] == f"com.meiva.fleet-update.mac.{_STAMP}"
    command = job["ProgramArguments"][2]
    assert 'H="$HOME/.meiva"; S="$H/source"' in command
    assert 'MEIVA_HOME="$H" "$S/.venv/bin/meiva" start' in command

    home = default_home / ".meiva"
    (home / "source").mkdir(parents=True)
    assert check_own_checkout(["start"], home / "source", home=home) is None
    assert mechanics.owns_os_job_home(
        "autostart", roots=JobRoots(home=home, repo=home / "source", user_home=default_home)
    )


# meiva:note
# A window runs from a bare operator checkout, whatever generation the cluster runs: importing
# every module of the package loads none of the application's own packages.
def test_the_updater_imports_no_application_code() -> None:
    done = subprocess.run(
        [sys.executable, "-S", str(Path(__file__).with_name("import_probe.py"))],
        env={"PYTHONPATH": str(repo_root())},
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.splitlines() == ["[]", "True", "[]", "cli.fleet_update.__main__"]


_ERROR_WITH_BACKSLASHES = r'["sh", "-c", "\$HOME\n"]'
_JOURNAL_WITH_BACKSLASHES = json.dumps(
    {"operation": "stop", "complete": True, "result": {"rc": 0, "error": _ERROR_WITH_BACKSLASHES}}
)


# meiva:note
# A host home the real PROBE script can read: a checkout with a venv python and a journal.
def _host_home(tmp_path: Path, journal: str | None) -> Path:
    source = tmp_path / ".meiva" / "source"
    (source / ".venv" / "bin").mkdir(parents=True)
    (source / ".venv" / "bin" / "python").symlink_to(sys.executable)
    git(source, "init", "-q", "--initial-branch=main")
    git(source, "commit", "-q", "--allow-empty", "-m", "base")
    (source / ".git" / "info" / "exclude").write_text(".venv/\n")
    if journal is not None:
        run = tmp_path / ".meiva" / "run"
        run.mkdir(parents=True)
        (run / "lifecycle-op.json").write_text(journal)
    return tmp_path / ".meiva" / "run" / "meiva-root" / "meiva-root.lock"


def _probed(shell: str, home: Path) -> dict[str, str]:
    result = subprocess.run(
        [shell, "-c", PROBE],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        key: value for key, _, value in (line.partition("=") for line in result.stdout.splitlines())
    }


@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/sh"])
def test_the_probe_carries_backslashes_in_the_journal_unchanged(shell: str, tmp_path: Path) -> None:
    if not Path(shell).exists():
        pytest.skip(f"{shell} is not installed")
    _host_home(tmp_path, _JOURNAL_WITH_BACKSLASHES)

    facts = _probed(shell, tmp_path)

    assert facts["head"] == git(tmp_path / ".meiva" / "source", "rev-parse", "HEAD")
    assert facts["dirty"] == "0"
    assert facts["op"] == _JOURNAL_WITH_BACKSLASHES
    assert json.loads(facts["op"])["result"]["error"] == _ERROR_WITH_BACKSLASHES


@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/sh"])
def test_the_probe_reads_whether_a_meiva_root_holds_its_instance_lock(
    shell: str, tmp_path: Path
) -> None:
    if not Path(shell).exists():
        pytest.skip(f"{shell} is not installed")
    lock = _host_home(tmp_path, None)
    assert _probed(shell, tmp_path)["root"] == "down", "no lock file: no root has ever run"
    lock.parent.mkdir(parents=True)
    held = instance_lock.acquire_instance_lock(lock.parent)
    try:
        assert _probed(shell, tmp_path)["root"] == "up"
    finally:
        instance_lock.release_instance_lock(held)
    assert _probed(shell, tmp_path)["root"] == "down"
    assert _probed(shell, tmp_path)["op"] == "", "no journal reads as an empty record"


@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/sh"])
def test_the_machine_name_is_asked_through_the_hosts_real_shell(shell: str, tmp_path: Path) -> None:
    if not Path(shell).exists():
        pytest.skip(f"{shell} is not installed")
    source = tmp_path / ".meiva" / "source"
    (source / "base" / "cluster" / "machine").mkdir(parents=True)
    for package in (
        source / "base",
        source / "base" / "cluster",
        source / "base" / "cluster" / "machine",
    ):
        (package / "__init__.py").write_text("")
    (source / "base" / "cluster" / "machine" / "configured.py").write_text(
        'class Identity:\n    def name(self) -> str:\n        return "mac-mini"\n'
        "def ConfiguredMachineIdentity(config) -> Identity:\n    return Identity()\n"
    )
    (source / "base" / "config.py").write_text("class ConfigBoot: pass\n")
    python = source / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)

    local_ssh = ssh_on_path(tmp_path / "bin", Path(__file__).with_name("local_ssh.py"))
    env = {
        "PATH": f"{local_ssh}{os.pathsep}{os.environ['PATH']}",
        "FLEET_LOCAL_SHELL": shell,
        "FLEET_LOCAL_HOME": str(tmp_path),
    }
    session = Session(
        log_dir=tmp_path / "logs",
        half="up",
        dry_run=False,
        repo=tmp_path,
        env=env,
        now=datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC),
        pacing=Pacing(),
        timers=ProcessTimers(),
    )
    try:
        names = fleet_verify.machine_names(session, ("mac",))
    finally:
        session.log.close()

    assert names == {"mac": "mac-mini"}
