"""`meiva lgtm on|off`: declare or retract this home as an observability station, then restart
the cluster so the desired services take effect.

The switch itself belongs to observability (`observability/lgtm.py`: the station marker, the
backend names); this module is the restart half — it drives `cmd_start`, which only lifecycle
may import — so it lives here, one level above observability, instead of the other way round.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from base.telemetry import EventPipeline
from base.telemetry.lgtm_local import BACKENDS
from cli.commands.lifecycle import start
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from services.supervision.healthchecks.lgtm import lgtm_host_marker


def selection_problem(
    *, enabled: bool, only_services: tuple[str, ...], disabled_services: tuple[str, ...]
) -> str | None:
    backends = set(BACKENDS)
    if enabled and backends & set(disabled_services):
        return "`lgtm on` cannot --disable-service an LGTM backend"
    if enabled and only_services and not backends <= set(only_services):
        return "`lgtm on` with --only-service must name every LGTM backend: " + ", ".join(
            sorted(BACKENDS)
        )
    if not enabled and backends & set(only_services):
        return "`lgtm off` cannot --only-service an LGTM backend"
    return None


def _reconcile(
    *,
    enabled: bool,
    only_services: tuple[str, ...],
    disabled_services: tuple[str, ...],
    all_services: bool,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
) -> int:
    # meiva:note
    # The operator names the start selection on the command line; nothing is derived from an
    # earlier start. The toggle refuses only a selection that contradicts it (backends disabled
    # while turning LGTM on, or named while turning it off).
    if problem := selection_problem(
        enabled=enabled, only_services=only_services, disabled_services=disabled_services
    ):
        raise ValueError(problem)
    return start.cmd_start(
        only_services=only_services,
        disabled_services=disabled_services,
        all_services=all_services,
        retained_children=retained_children,
        database_factory=database_factory,
        producer=producer,
        command=command,
    )


def cmd_lgtm_on(
    *,
    only_services: tuple[str, ...],
    disabled_services: tuple[str, ...],
    all_services: bool,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
) -> int:
    # meiva:note
    # Declare this home an observability station and start the named selection.
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    if problem := selection_problem(
        enabled=True, only_services=only_services, disabled_services=disabled_services
    ):
        raise ValueError(problem)
    marker = lgtm_host_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch(exist_ok=True)
    return _reconcile(
        enabled=True,
        only_services=only_services,
        disabled_services=disabled_services,
        all_services=all_services,
        retained_children=retained_children,
        database_factory=database_factory,
        producer=producer,
        command=command,
    )


def cmd_lgtm_off(
    *,
    only_services: tuple[str, ...],
    disabled_services: tuple[str, ...],
    all_services: bool,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
) -> int:
    # meiva:note
    # Retract the station marker and start the named selection, preserving observation data.
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    if problem := selection_problem(
        enabled=False, only_services=only_services, disabled_services=disabled_services
    ):
        raise ValueError(problem)
    lgtm_host_marker().unlink(missing_ok=True)
    return _reconcile(
        enabled=False,
        only_services=only_services,
        disabled_services=disabled_services,
        all_services=all_services,
        retained_children=retained_children,
        database_factory=database_factory,
        producer=producer,
        command=command,
    )
