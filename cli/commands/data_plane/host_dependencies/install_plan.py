# meiva:note
# How one provisioning group is installed, and how its install is worded.
#
# The group's script lives under `scripts/provision/` and runs once per group through `bash`; the
# gate shell checks the script exists, runs `argv`, and prints the lines returned here. The
# package manager named in the announcement is the platform's: Homebrew on macOS, apt elsewhere.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cli.commands.data_plane.host_dependencies.table import (
    Component,
    provision_label,
    provision_script,
)

__all__ = [
    "InstallStep",
    "exit_line",
    "launch_failure_line",
    "missing_script_line",
    "plan_install",
]


# meiva:note
# One group's install: the script, the command that runs it and the line announcing it.
@dataclass(frozen=True)
class InstallStep:
    script: Path
    argv: tuple[str, ...]
    announcement: str


# meiva:note
# The install of `group` from the checkout at `repo`.
def plan_install(group: Component, *, repo: Path, macos: bool) -> InstallStep:
    script = repo / "scripts" / "provision" / provision_script(group)
    manager = "Homebrew" if macos else "apt"
    return InstallStep(
        script=script,
        argv=("bash", str(script)),
        announcement=(
            f"  · installing {provision_label(group)} with {manager} "
            "(this can take several minutes)"
        ),
    )


# meiva:note
# The stderr line for a group whose script is not in the checkout.
def missing_script_line(script: Path) -> str:
    return f"  ✗ provision script missing: {script}"


# meiva:note
# The stderr line for a script that could not be started or timed out.
def launch_failure_line(script: Path, error: str) -> str:
    return f"  ✗ {script.name} failed to run: {error}"


# meiva:note
# The stderr line for a script that ran and exited non-zero: the gate reads the host again anyway.
def exit_line(script: Path, returncode: int) -> str:
    return f"  ! {script.name} exited {returncode}; re-checking what landed"
