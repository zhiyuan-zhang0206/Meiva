"""Configuration slice of the events-maintenance daemon, and what its passes share.

Fields keep their flat registry names. `services/upkeep/events_maintenance/daemon.py` (the
composition root) builds
this slice from the supplied ConfigBoot and hands it to the loops and passes. See `future/infra/security/dependency-injection.md`.

The module also owns the fact the daemon and the operator commands both need and neither may
import from the other: the database handle built from a boot owner.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot


@dataclass(frozen=True)
class EventsMaintenanceConfig:
    events_maintenance_interval_seconds: float
    events_maintenance_pass_deadline_s: float
    timezone: str


def events_maintenance_db(
    config: ConfigBoot, *, gate: ProcessDbGate, local_host: Callable[[], str], home: Path
) -> Database:
    """The handle on the cluster database, built where the daemon (or one of its operator
    commands) starts."""
    return Database(
        db_config_from_boot(config),
        gate=gate,
        direct_url=ClusterDirectUrl(local_host, home=home),
    )
