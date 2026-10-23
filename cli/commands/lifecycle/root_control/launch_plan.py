# meiva:note
# How a root is launched: its inputs, its command line and the seed a keeper holds.
#
# `RootLaunchInputs` is what a launch needs from the process, read once by the start entry
# and handed down. `plan_root_launch` and `keeper_seed` turn the interpreter, the working
# directory and the run directory into the command line, log paths and seed that the
# direct spawn and the helper keeper both execute.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from base.host.permissions_helper.wire import RootSeedConfig

__all__ = ["RootLaunch", "RootLaunchInputs", "keeper_seed", "plan_root_launch"]

_ROOT_MODULE = "services.supervision.meiva_root"
_WIRING_REF = "services.supervision.meiva_root_glue.glue:build_wiring"
_ROOT_STDOUT_LOG = "root.stdout.log"
_ROOT_STDERR_LOG = "root.stderr.log"


# meiva:note
# What one root launch reads from the process.
#
# `service_path` is the home's declared service PATH (`settings.general.service_path`)
# and `remote_data_plane` whether the data plane is remote-managed. The start entry
# reads them once; nothing below it reads settings.
@dataclass(frozen=True)
class RootLaunchInputs:
    home: Path
    service_path: str
    remote_data_plane: bool


# meiva:note
# The daemon command line, the manifests it loads and where its output goes.
@dataclass(frozen=True)
class RootLaunch:
    argv: tuple[str, ...]
    cwd: Path
    run_dir: Path
    manifests: Path
    stdout_log: Path
    stderr_log: Path


# meiva:note
# The K3 launch face the root package freezes: its module, run dir, manifests, wiring.
def plan_root_launch(*, interpreter: Path, cwd: Path, run_dir: Path, manifests: Path) -> RootLaunch:
    argv = (
        str(interpreter),
        "-m",
        _ROOT_MODULE,
        "--run-dir",
        str(run_dir),
        "--manifests",
        str(manifests),
        "--wiring",
        _WIRING_REF,
    )
    return RootLaunch(
        argv=argv,
        cwd=cwd,
        run_dir=run_dir,
        manifests=manifests,
        stdout_log=run_dir / _ROOT_STDOUT_LOG,
        stderr_log=run_dir / _ROOT_STDERR_LOG,
    )


# meiva:note
# The keeper's launch config for `launch`: all paths absolute, `env` for the root.
def keeper_seed(launch: RootLaunch, env: Mapping[str, str]) -> RootSeedConfig:
    return {
        "argv": list(launch.argv),
        "cwd": str(launch.cwd),
        "run_dir": str(launch.run_dir),
        "stdout": str(launch.stdout_log),
        "stderr": str(launch.stderr_log),
        "env": dict(env),
    }
