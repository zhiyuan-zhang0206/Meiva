# meiva:note
# Inputs shared by the root-control shell tests: services, a source checkout, a helper that
# vouches for this test process, and a stand-in interpreter for the root daemon.

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import psutil

from base.cluster.machine import MachineRole
from base.daemon.health import DaemonProbe
from base.native_process.ownership import OwnedProcess
from base.paths import root_run_dir
from base.sessions.tests.scripted_helper import ScriptedHelper
from cli.commands.lifecycle.root_control.launch_plan import RootLaunchInputs
from services.supervision.roster.service_spec import ServiceSpec

__all__ = [
    "custody_helper",
    "fake_interpreter",
    "git_repo",
    "launch_inputs",
    "service",
    "this_process",
]

# meiva:note
# Both capabilities and no database: the launcher delivers this service no login, no API
# token and no profile marker, so its manifest needs no ledger in the home under test.
_NEUTRAL: frozenset[MachineRole] = frozenset({"gateway", "agent-runner"})


# meiva:note
# A service whose identity probe is `probe` (none when None).
def service(
    session: str = "gateway", *, probe: Callable[[], DaemonProbe] | None = None
) -> ServiceSpec:
    return ServiceSpec(
        session=session,
        cmd="python -m service",
        capabilities=_NEUTRAL,
        requires_db=False,
        identity_probe=probe,
    )


def launch_inputs(home: Path) -> RootLaunchInputs:
    return RootLaunchInputs(
        home=home,
        service_path=str(home / "tools"),
        remote_data_plane=False,
        environment={"HOME": str(home)},
    )


# meiva:note
# A committed checkout: what `source_digest` binds a launch to.
def git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "app.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "source fixture",
        ],
        check=True,
    )
    return path


# meiva:note
# An executable the launcher runs where it would run Python: `<it> -m <module> ...`.
def fake_interpreter(directory: Path, body: str) -> Path:
    script = directory / "fake-python"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return script


# meiva:note
# A permissions helper that vouches for `root` as its own child in `home`.
#
# The helper's pid is `root`'s parent, so the custody check, which asks whether the root was
# started by this helper, passes for any live process.
def custody_helper(home: Path, root: OwnedProcess) -> ScriptedHelper:
    return ScriptedHelper(
        ping={
            "pong": True,
            "root_stop_intent_v1": True,
            "helper_shutdown_v1": True,
            "pid": psutil.Process(root.pid).ppid(),
        },
        root_status={"pid": root.pid, "run_dir": str(root_run_dir(home))},
    )


def this_process() -> OwnedProcess:
    return OwnedProcess.capture(psutil.Process(os.getpid()))
