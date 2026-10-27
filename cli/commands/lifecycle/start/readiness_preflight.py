# meiva:note
# Read-only `meiva start` state checks, run before a stop that a start must follow.
#
# Restart checks the coming start's prerequisites while the current services still serve. A
# local-state refusal leaves the running generation intact.
#
# This module is the local-state half of "validate before kill": the read-only parts of what
# start checks, moved in front of the stop, so a failure refuses the stop while the host still
# serves. It reads the facts; `preflight_findings` decides which are fatal and words them.
#
# What it forwards (each item read-only; nothing here repairs or launches):
#
# - the `$MEIVA_HOME` private-tree skeleton: a `logs` / `workspaces` / `memory` root that
#   converge would ABORT on (a symlink, or not a directory), and the
#   `.metadata_never_index` marker whose converge write requires a regular file.
#   Non-regular nodes INSIDE the trees (sockets, FIFOs, devices) are reported as observations
#   only: converge skips them, so start survives them;
# - daemon health ports another home's daemon already answers on, the blocking pre-bind gate of
#   `refuse_occupied_health_ports` (issue #977), which otherwise runs only after the stop;
# - tracked migration files in the checked-out tree that cannot be read: the applier opens them
#   inside start, and the pre-stop layout gate (`validate_migrations_at_ref`) vets names only;
# - the two start prerequisites no other pre-stop gate covers: the prod-checkout anchoring rule,
#   and the venv entry points: `.venv/bin/python` (what every service session launches through)
#   always, `.venv/bin/meiva` (what an external start process executes) only when the caller's
#   start would exec it (`check_launcher`).
#
# Contract: read-only, never raises for a finding (findings are data). Returns 0 to proceed with
# restart, 1 to refuse it. The caller answers a refusal with RESTART_DECLINED ("nothing was
# stopped, host still serving"): unlike the migrations-layout gate there is no revert, because the
# target tree is not at fault, the host's local state is, and a retry re-checks it.

from __future__ import annotations

import os
import sys
from pathlib import Path

from base.cluster.machine import MachineIdentityReader, MachineRoles
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.host.private_storage import (
    private_file_problem,
    private_tree_root_problem,
    scan_non_regular_nodes,
)
from base.host.system.hostport import Host
from base.paths.home import home_checkout_error
from cli.commands.lifecycle.start.preflight_findings import (
    EntrypointFacts,
    PortFacts,
    PortOccupant,
    PreflightFacts,
    TreeFacts,
    preflight_report,
)

_TREE_ROOTS = ("logs", "workspaces", "memory")
_LOOSE_DIRECTORIES = ("configs", "secrets")


# meiva:note
# Vet the local state the coming `meiva start` needs, before the stop.
#
# 0 = proceed (any observations are printed); 1 = refuse, with every finding printed.
# See the module docstring for what is checked and why.
#
# `check_launcher=False` drops the `.venv/bin/meiva` entry-point check for a caller whose
# start runs in-process and never execs it (`meiva restart`); development's
# `.venv/bin/python` is still checked.
def preflight_start_readiness(
    repo: Path,
    *,
    home: Path,
    check_launcher: bool,
    only_services: tuple[str, ...],
    disabled_services: tuple[str, ...],
    all_services: bool,
    machine: MachineIdentityReader,
    host: Host,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> int:
    migration_error, unreadable = _migration_facts(migrations_dir)
    facts = PreflightFacts(
        checkout_problem=home_checkout_error(repo, home=home),
        ports=_port_facts(
            only_services=only_services,
            disabled_services=disabled_services,
            all_services=all_services,
            machine=machine,
            host=host,
            home=home,
        ),
        trees=tuple(_tree_facts(home / name) for name in _TREE_ROOTS),
        not_directories=_not_directories(home),
        marker=str(_marker(home)),
        marker_problem=private_file_problem(_marker(home)),
        migration_error=migration_error,
        unreadable_migrations=unreadable,
        entrypoints=_entrypoints(repo, check_launcher=check_launcher),
    )
    report = preflight_report(facts)
    for line in report.stdout:
        print(line)
    for line in report.stderr:
        print(line, file=sys.stderr)
    return report.exit_code


def _marker(home: Path) -> Path:
    return home / ".metadata_never_index"


# meiva:note
# The health ports another home's daemon holds, on the roster the start will launch.
#
# Detection failures are facts, not refusals: a preflight must never be the thing that takes
# the host down.
def _port_facts(
    *,
    only_services: tuple[str, ...],
    disabled_services: tuple[str, ...],
    all_services: bool,
    machine: MachineIdentityReader,
    host: Host,
    home: Path,
) -> PortFacts:
    import cli.commands._repo as _repo_commands

    roles = _repo_commands.roles_or_none(machine=machine)
    if roles is None:
        return PortFacts(roles_resolved=False, check_error=None, occupied=())
    try:
        occupied = _occupied(
            roles,
            only_services=only_services,
            disabled_services=disabled_services,
            all_services=all_services,
            machine=machine,
            host=host,
            home=home,
        )
    # meiva:note
    # a preflight must not fail the update
    except Exception as exc:
        return PortFacts(roles_resolved=True, check_error=str(exc), occupied=())
    return PortFacts(roles_resolved=True, check_error=None, occupied=occupied)


def _occupied(
    roles: MachineRoles,
    *,
    only_services: tuple[str, ...],
    disabled_services: tuple[str, ...],
    all_services: bool,
    machine: MachineIdentityReader,
    host: Host,
    home: Path,
) -> tuple[PortOccupant, ...]:
    from base.deploy.lifecycle.service_selection import resolve_selection
    from cli.commands.lifecycle.root_driver import start_roster
    from cli.commands.probe import occupied_health_ports
    from services.supervision.roster.composition import build_services
    from services.supervision.roster.spec import services_for_capabilities_annotated

    available = {s.session for s in build_services(home=home)}
    roster = start_roster(
        services_for_capabilities_annotated(roles, machine=machine, host=host, home=home),
        resolve_selection(
            available,
            only=only_services,
            excluded=disabled_services,
            all_services=all_services,
        ),
    )
    return tuple(
        PortOccupant(port.spec.session, port.detail) for port in occupied_health_ports(roster)
    )


# meiva:note
# One private tree: whether converge could use its root and what it would skip inside.
def _tree_facts(root: Path) -> TreeFacts:
    try:
        skipped = tuple(str(node) for node in scan_non_regular_nodes(root))
        scan_error = None
    except OSError as exc:
        skipped = ()
        scan_error = str(exc)
    return TreeFacts(
        root=str(root),
        root_problem=private_tree_root_problem(root),
        skipped=skipped,
        scan_error=scan_error,
    )


# meiva:note
# `configs` / `secrets` that exist as something converge's mkdir cannot tolerate.
#
# A symlink TO a directory is tolerated (`mkdir(exist_ok=True)`); a file or a dangling link
# is not.
def _not_directories(home: Path) -> tuple[str, ...]:
    return tuple(
        str(path)
        for path in (home / name for name in _LOOSE_DIRECTORIES)
        if (path.is_symlink() or path.exists()) and not path.is_dir()
    )


# meiva:note
# (enumeration error, tracked migration files this checkout cannot read).
#
# `validate_migrations_at_ref` already vets the target's names before the stop; this asks the
# next question the applier will ask: can the file be opened.
def _migration_facts(migrations_dir: Path) -> tuple[str | None, tuple[tuple[str, str], ...]]:
    from base.deploy.schema.migrations import MigrationLayoutError, unreadable_migration_files

    try:
        return None, tuple(unreadable_migration_files(migrations_dir=migrations_dir))
    except MigrationLayoutError as exc:
        return str(exc), ()


# meiva:note
# The venv entry points the coming start needs, keyed to who runs it.
#
# - `.venv/bin/python`: the interpreter every service session launches through. Checked for
#   every caller: `meiva restart` has no `uv sync` verification behind it, so a damaged venv
#   is exactly the "stopped and cannot come back" class this gate exists for.
# - `.venv/bin/meiva`: the launcher the update leg's step 5 execs; a file that exists but
#   cannot be executed makes its `subprocess.run` raise PermissionError. Presence is vetted
#   by step 3.5; this adds the exec bit. Skipped where the start is in-process.
def _entrypoints(repo: Path, *, check_launcher: bool) -> tuple[EntrypointFacts, ...]:
    from base.host.system.backend import get_backend

    backend = get_backend()
    interpreter = backend.venv_launcher("python", root=repo)
    entries = [
        _entrypoint(
            interpreter,
            label="the interpreter every service session launches through",
            fix="re-run `uv sync`",
        )
    ]
    launcher = backend.venv_launcher("meiva", root=repo)
    # meiva:note
    # a missing launcher is step 3.5's report
    if check_launcher and launcher.is_file():
        entries.append(
            _entrypoint(
                launcher,
                label="the launcher the update leg execs next",
                fix="`chmod +x` it, or re-run `uv sync`",
            )
        )
    return tuple(entries)


def _entrypoint(binary: Path, *, label: str, fix: str) -> EntrypointFacts:
    return EntrypointFacts(
        path=str(binary),
        label=label,
        fix=fix,
        is_file=binary.is_file(),
        executable=os.access(binary, os.X_OK),
    )
