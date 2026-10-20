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


def _reconcile(
    *,
    enabled: bool,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
) -> int:
    # meiva:note
    # Change only the backends; every other launched service is named as it runs.
    #
    # The selection is the set the last start launched (the root's manifest) with
    # the backends added or removed, passed to start as an explicit `only` list.
    from base.paths import meiva_home, root_manifests_path
    from services.supervision.meiva_root_glue.manifests import launched_services

    launched = launched_services(root_manifests_path(meiva_home()))
    if launched is None:
        raise RuntimeError(
            "no start has launched this unit yet; run `meiva start` with an explicit "
            "service selection first"
        )
    names = set(launched) | set(BACKENDS) if enabled else set(launched) - set(BACKENDS)
    if not names:
        # meiva:note
        # Turning off a tree of only LGTM leaves an explicit empty desired tree.
        from services.supervision.roster import build_services

        return start.cmd_start(
            only_services=(),
            disabled_services=tuple(s.session for s in build_services()),
            all_services=False,
            retained_children=retained_children,
            database_factory=database_factory,
            producer=producer,
            command=command,
        )
    return start.cmd_start(
        only_services=tuple(sorted(names)),
        disabled_services=(),
        all_services=False,
        retained_children=retained_children,
        database_factory=database_factory,
        producer=producer,
        command=command,
    )


def cmd_lgtm_on(
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
) -> int:
    """Declare this home an observability station and reconcile normal start."""
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    marker = lgtm_host_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch(exist_ok=True)
    return _reconcile(
        enabled=True,
        retained_children=retained_children,
        database_factory=database_factory,
        producer=producer,
        command=command,
    )


def cmd_lgtm_off(
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
) -> int:
    """Disable the backend units, preserving observation data and other services."""
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    lgtm_host_marker().unlink(missing_ok=True)
    return _reconcile(
        enabled=False,
        retained_children=retained_children,
        database_factory=database_factory,
        producer=producer,
        command=command,
    )
