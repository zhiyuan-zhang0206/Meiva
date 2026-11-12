# meiva:note
# Whether the application root now running on this host could serve a start of this checkout.
#
# `meiva start` over a live root refuses when the requested launch differs from the running one
# (`root_driver.admit_live_start`), and that refusal comes only after a window has stopped every
# service and switched the checkout. A fleet update that keeps the root (`--keep-shell-sessions`
# or a kept service) therefore runs this module from a throwaway worktree of the target commit,
# before anything stops, to learn the verdict while the cluster still serves. It is a read: it
# asks the root for its status and builds the manifest a start would request, and changes nothing.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.md

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from base.sessions.env_forwarding import managed_service_env
from cli.commands.lifecycle import root_driver
from cli.commands.lifecycle.start import launch_roster, read_launch_inputs
from cli.commands.lifecycle.start.start_config import read_start_config
from cli.database.runtime import CommandRuntime

__all__ = ["ROOT_NONE", "ROOT_SAME", "main", "root_generation_verdict"]

ROOT_NONE = "none"
ROOT_SAME = "same"
_CHANGED = "changed: "
_SELECTIONS = ("--all-services", "--only-service", "--disable-service")


# meiva:note
# `none` when no root runs, `same` when a start of this checkout with this selection would reuse
# the running root, otherwise `changed: ` and the refusal that start would print.
def root_generation_verdict(
    command: CommandRuntime, *, repo: Path, selection: str, names: Sequence[str]
) -> str:
    config = read_start_config(command.config)
    inputs = read_launch_inputs(command, config)
    snapshot = root_driver.root_status(
        root_driver.root_client(inputs.home, environ=inputs.environment, pid=command.pid)
    )
    if snapshot is None:
        return ROOT_NONE
    roles = command.machine.role()
    roster = launch_roster(
        roles,
        only_services=tuple(names) if selection == "--only-service" else (),
        disabled_services=tuple(names) if selection == "--disable-service" else (),
        all_services=selection == "--all-services",
        machine=command.machine,
        host=command.host,
        home=command.home,
    )
    # meiva:note
    # The reader imports NEW from a disposable checkout; the future start still
    # runs in repo, whose venv must supply both PATH and VIRTUAL_ENV.
    env = managed_service_env(inputs.service_path, environ=inputs.environment, venv=repo / ".venv")
    manifest = root_driver.launch_manifest(roster, repo, roles=roles, env=env, inputs=inputs)
    try:
        root_driver.require_same_generation(manifest, snapshot, reconcile=True)
    except root_driver.RootDriverError as refusal:
        return f"{_CHANGED}{refusal}"
    return ROOT_SAME


# meiva:note
# The program a window runs on a host (`home` is the host's own): `ROOT_GENERATION=<verdict>` for
# the selection it names.
#
# `repo` is the checkout the real start runs in (the host's `source`), not the worktree this runs
# from: the units' working directory is part of their manifest, so only that path compares equal.
def main(argv: Sequence[str], *, home: Path) -> int:
    repo, selection, *names = argv
    if selection not in _SELECTIONS:
        raise ValueError(f"unknown service selection {selection!r}")
    command = CommandRuntime.create(home)
    verdict = root_generation_verdict(command, repo=Path(repo), selection=selection, names=names)
    print(f"ROOT_GENERATION={verdict}")
    return 0
