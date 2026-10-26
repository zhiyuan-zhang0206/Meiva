# meiva:note
# The converge step contract — the types every converge step is written against.
#
# Split out of ``converge_host.py`` so the step implementations can live in more than one
# module without an import cycle: a step module imports this, and ``converge_host.py``
# imports the step modules.

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from base.agents.context.clients import DatabaseFactory
from base.clock import Clock, Timers
from base.cluster.machine import MachineIdentityReader, MachineRole, MachineRoles
from base.config import ConfigBoot
from base.host.permissions_helper.client import HelperFactory
from base.host.system.hostport import Host
from base.host.system.job_values import JobRoots
from base.telemetry import EventPipeline
from cli.database.runtime import UserSession

ALL_ROLES: frozenset[MachineRole] = frozenset({"gateway", "agent-runner", "observability-station"})

# The same capabilities in a deterministic order, for steps that fan out over
# whichever ones a unit carries. A frozenset has no order, and `sorted()` over
# one widens the `MachineRole` literal back to `str`.
CAPABILITY_ORDER: tuple[MachineRole, ...] = ("agent-runner", "gateway")


@dataclass(frozen=True)
class ConvergeCtx:
    repo: Path
    meiva_home: Path
    roles: MachineRoles | None  # None = unit not configured yet (fresh install)
    config: ConfigBoot
    machine: MachineIdentityReader
    database_factory: DatabaseFactory
    producer: Callable[[], EventPipeline]
    # The permissions-helper factory, or None on a platform that has no helper (Linux). None
    # never means "skip the helper": a platform that has one always carries it, and a step that
    # needs it calls `require_helper`.
    helper: HelperFactory | None
    host: Host
    session: UserSession
    timers: Timers
    # meiva:note
    # The wall clock of the invocation that runs this converge; a step stamps with `clock.now()`.
    clock: Clock
    services: frozenset[str]

    # meiva:note
    # The home, checkout and account home every OS job of this converge is anchored to.
    @property
    def job_roots(self) -> JobRoots:
        return JobRoots(home=self.meiva_home, repo=self.repo, user_home=self.session.user_home)

    def require_helper(self) -> HelperFactory:
        """The permissions-helper factory, for a step that only runs where a helper exists."""
        if self.helper is None:
            raise RuntimeError(
                "this converge step needs the permissions helper, but this platform has none"
            )
        return self.helper

    def read_config(self) -> ConfigBoot:
        """Read startup inputs on first use, retaining an explicitly prepared owner."""
        if not self.config.prepared:
            self.config.read_process_environment()
        return self.config


@dataclass(frozen=True)
class ConvergeStep:
    name: str
    apply: Callable[[ConvergeCtx], None]
    roles: frozenset[MachineRole] = ALL_ROLES
    requires_unit_config: bool = False
    # host-global wiring (the single `~/.local/bin/meiva` symlink + shell-rc PATH
    # edit) belongs to the host's prod install, not to any one cluster. Skipped
    # for a dev / non-default cluster so a worktree's `meiva start` never repoints
    # the host's prod `meiva` or rewrites the shell rc.
    host_global: bool = False
    # Empty means shared host/cluster preparation. Otherwise one selected
    # consumer is required; role membership alone does not select a service.
    services: frozenset[str] = frozenset()
