# meiva:note
# The preparation of a start that finds no live application root, run against the real host.
#
# What the preparation is and in which order is `cold_start_plan.plan_cold_start`'s; this
# module runs each step of that plan and stops at the first one that fails. A step that
# leaves the data plane or the schema untouched is recorded in the start trace all the same,
# so the trace shows the plan that ran.

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from base.cluster.machine import MachineRoles
from base.deploy import start_trace
from base.native_process.root_control.ipc import UnitState
from base.paths import skills_dir
from base.telemetry import EventPipeline
from cli.commands import _repo
from cli.commands.converge.spec import ConvergeAdvisoryError, ConvergeStep
from cli.commands.data_plane import bringup, completion
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.extensions import materialize
from cli.commands.lifecycle import converge_host, root_driver
from cli.commands.lifecycle.root_control.snapshot import root_units
from cli.commands.lifecycle.start.cold_start_plan import ColdStep, plan_cold_start
from cli.commands.lifecycle.start.migrations import cmd_migrations_apply
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from services.supervision.roster.service_spec import ServiceSpec

__all__ = [
    "ColdStart",
    "cold_start_steps",
    "live_schema_behind",
    "prepare_cold_start",
    "prepare_live_migrations",
    "prepare_live_start",
    "running_db_units",
]


# meiva:note
# The host-dependency step is deliberately first: a missing redis/pgbouncer/node
# or crontab/systemctl/curl must refuse before any green converge step or service
# launch, not after them (a host without crontab used to die at the WAL-G job).
def cold_start_steps() -> tuple[ConvergeStep, ...]:
    from cli.commands.data_plane import host_dependencies

    return (
        ConvergeStep(
            "host dependencies",
            host_dependencies.ensure_host_dependencies_step,
            roles=frozenset({"gateway", "agent-runner"}),
        ),
        *converge_host.CONVERGE_STEPS,
    )


# meiva:note
# What the preparation reads, handed down by the start: the invocation, the checkout it
# converges to, the roles it prepares for, the data plane it configured and the converge
# steps it runs first (the start passes `cold_start_steps()`; a step table is data).
@dataclass(frozen=True)
class ColdStart:
    command: CommandRuntime
    database_factory: OperatorDatabaseFactory
    producer: Callable[[], EventPipeline]
    repo: Path
    roles: MachineRoles
    plane: DataPlaneConfig
    converge_steps: tuple[ConvergeStep, ...]
    # meiva:note
    # A live-root start runs the table minus `skipped_while_root_runs` steps (`prepare_live_start`).
    partial_converge: bool = False


# meiva:note
# Prepare storage and configuration; the first failing step's non-zero code ends the start.
#
# `retained_children` is the caller's registry of the processes it spawns, kept until its
# shutdown: the data-plane step appends the instance it starts, so it is handed in beside the
# frozen `cold` rather than held by it.
def prepare_cold_start(
    cold: ColdStart,
    roster: tuple[ServiceSpec, ...],
    *,
    retained_children: list[subprocess.Popen[bytes]],
) -> int:
    for step in plan_cold_start(cold.roles):
        with start_trace.phase(step.value):
            rc = _run_step(step, cold, roster, retained_children)
        if rc != 0:
            return rc
    return 0


# meiva:note
# The preparation of a start that finds the application root running: the converge table
# without the steps that cannot run while the root does (`cold.converge_steps` carries it), none
# of the storage preparation.
def prepare_live_start(cold: ColdStart, roster: tuple[ServiceSpec, ...]) -> int:
    with start_trace.phase("converge host (live root)"):
        return _run_step(ColdStep.CONVERGE, cold, roster, [])


# meiva:note
# Whether the database lacks migrations this checkout carries (read-only).
#
# Any other verdict, including a database ahead of the code, is the schema check's to report right
# after; an unreachable database raises here exactly as that check would.
def live_schema_behind(db_url: str) -> bool:
    from base.deploy.schema.migrations import (
        CodeBehindSchema,
        SchemaVersionMismatch,
        assert_schema_current,
    )

    try:
        assert_schema_current(db_url)
    except SchemaVersionMismatch:
        return True
    except CodeBehindSchema:
        return False
    return False


# meiva:note
# The requested units that use the database and run under the live root now.
def running_db_units(
    roster: tuple[ServiceSpec, ...], *, home: Path, environ: Mapping[str, str], pid: int
) -> tuple[str, ...]:
    snapshot = root_driver.root_status(root_driver.root_client(home, environ=environ, pid=pid))
    if snapshot is None:
        return ()
    units = root_units(snapshot)
    return tuple(
        spec.session
        for spec in roster
        if spec.requires_db
        and (unit := units.get(spec.session)) is not None
        and unit["state"] == UnitState.RUNNING.value
    )


# meiva:note
# Run `steps` (`plan_live_migration`) to apply the migrations a live root's database is missing.
#
# A migration changes the schema under whatever still uses it, so it runs only when the root hosts
# no database-using unit (a stop that keeps the shell sessions stops the rest): a unit still
# running is a writer of the old schema and the start refuses with its name. A failed migration
# fails the start like the cold path's.
def prepare_live_migrations(
    cold: ColdStart,
    roster: tuple[ServiceSpec, ...],
    *,
    steps: tuple[ColdStep, ...],
    running: tuple[str, ...],
    retained_children: list[subprocess.Popen[bytes]],
) -> int:
    if running:
        print(
            "  ✗ the database is missing migrations this checkout carries, and the running root "
            f"still hosts services that use it: {', '.join(running)}. A migration needs them "
            "stopped: run `meiva stop` for the whole host (without keeping those services) and "
            "start again",
            file=sys.stderr,
        )
        return 1
    for step in steps:
        with start_trace.phase(f"{step.value} (live root)"):
            rc = _run_step(step, cold, roster, retained_children)
        if rc != 0:
            return rc
    return 0


def _run_step(
    step: ColdStep,
    cold: ColdStart,
    roster: tuple[ServiceSpec, ...],
    retained_children: list[subprocess.Popen[bytes]],
) -> int:
    command = cold.command
    match step:
        case ColdStep.CONVERGE:
            # meiva:note
            # Converge host state (symlink / PATH / $MEIVA_HOME dirs / plugin config
            # images). Memory initialization is explicit (`meiva memory init`).
            try:
                converge_host.converge_host(
                    cold.repo,
                    cold.roles,
                    services=frozenset(spec.session for spec in roster),
                    database_factory=cold.database_factory,
                    producer=cold.producer,
                    command=command,
                    steps=cold.converge_steps,
                    partial=cold.partial_converge,
                )
            except ConvergeAdvisoryError as e:
                # meiva:note
                # Advisory steps are host preparation an optional consumer needs; the host
                # itself is fully prepared, so the start continues. The exit code of the
                # standalone `meiva converge` still reports it.
                print(f"  ! converge finished with warnings: {e}", file=sys.stderr)
                return 0
            except Exception as e:
                print(f"  ✗ converge failed: {e}", file=sys.stderr)
                return 1
            return 0
        case ColdStep.DATA_PLANE:
            # meiva:note
            # A gateway brings up this cluster's own pg/redis instance (under its
            # $MEIVA_HOME, on its recorded ports). No docker on any POSIX platform.
            return bringup.ensure_gateway_data_plane(
                cold.plane,
                home=command.home,
                host=command.host,
                system=command.host.platform.system,
                retained_children=retained_children,
                database_factory=cold.database_factory,
                machine=command.machine,
                timers=command.timers,
                environ=command.config.process_environment(),
            )
        case ColdStep.GATEWAY_SCHEMA:
            bringup.prepare_gateway_schema(cold.plane, home=command.home)
            return 0
        case ColdStep.SKIP_LOCAL_SERVICES:
            print("\n→ local services: skipped (agent-runner uses central node's DB/Redis)")
            return 0
        case ColdStep.MIGRATIONS:
            return _apply_migrations(cold)
        case ColdStep.POOLER:
            completion.complete_gateway_data_plane(
                cold.plane,
                home=command.home,
                environment=command.config.process_environment(),
                database_factory=cold.database_factory,
                host=command.host,
                machine=command.machine,
                macos=command.host.platform.system == "darwin",
                timers=command.timers,
                secure_rng=command.secure_rng,
            )
            return 0
        case ColdStep.SCHEMA_CHECK:
            return _repo.assert_schema_current_or_die(cold.database_factory.db_url())
        case ColdStep.ADOPT_EXTENSIONS:
            # meiva:note
            # Adopt first: a name this machine installed before the registry existed is
            # invisible to the materializer until it has a row.
            materialize.adopt_local_extensions(
                database_factory=cold.database_factory,
                machine=command.machine,
                skills_root=skills_dir(command.home),
                home=command.home,
            )
            return 0
        case ColdStep.MATERIALIZE_EXTENSIONS:
            materialize.materialize_cluster_extensions(
                database_factory=cold.database_factory,
                skills_root=skills_dir(command.home),
                home=command.home,
            )
            return 0
        case _:
            assert_never(step)


def _apply_migrations(cold: ColdStart) -> int:
    print("\n→ apply pending migrations")
    try:
        cmd_migrations_apply(
            database_factory=cold.database_factory,
            machine=cold.command.machine,
            config=cold.plane,
            home=cold.command.home,
        )
    except Exception as e:
        print(f"  ✗ migrations apply failed: {e}", file=sys.stderr)
        return 1
    return 0
