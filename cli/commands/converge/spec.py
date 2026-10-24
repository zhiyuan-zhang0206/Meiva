# meiva:note
# The converge step contract — the types every converge step is written against.
#
# Split out of ``converge_host.py`` so the step implementations can live in more than one
# module without an import cycle: a step module imports this, and ``converge_host.py``
# imports the step modules.

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from base.agents.context.clients import DatabaseFactory
from base.clock import Timers
from base.cluster.machine import MachineIdentity, MachineRole, MachineRoles
from base.config import ConfigBoot
from base.host.permissions_helper.client import HelperFactory
from base.host.system.hostport import Host
from base.telemetry import EventPipeline

ALL_ROLES: frozenset[MachineRole] = frozenset({"gateway", "agent-runner", "observability-station"})

# The same capabilities in a deterministic order, for steps that fan out over
# whichever ones a unit carries. A frozenset has no order, and `sorted()` over
# one widens the `MachineRole` literal back to `str`.
CAPABILITY_ORDER: tuple[MachineRole, ...] = ("agent-runner", "gateway")


# meiva:note
# The invoking user's session, read once where converge is composed.
#
# A step that wires the user's account (the `~/.local/bin` link, the shell rc file) or
# asks a person something reads these values, so a test hands in the session it wants.
@dataclass(frozen=True)
class UserSession:
    user_home: Path
    # meiva:note
    # The login shell path (`$SHELL`); empty when the session names none.
    login_shell: str
    # meiva:note
    # Whether a human is at the terminal: both stdin and stdout are TTYs. Watchdog respawns
    # and rollout-driven starts have none.
    interactive: bool

    # meiva:note
    # This process's session; the composition point calls it once per operation.
    @classmethod
    def current(cls) -> UserSession:
        return cls(
            user_home=Path.home(),
            login_shell=os.environ.get("SHELL", ""),
            interactive=bool(
                sys.stdin and sys.stdout and sys.stdin.isatty() and sys.stdout.isatty()
            ),
        )


@dataclass(frozen=True)
class ConvergeCtx:
    repo: Path
    meiva_home: Path
    roles: MachineRoles | None  # None = unit not configured yet (fresh install)
    config: ConfigBoot
    machine: MachineIdentity
    database_factory: DatabaseFactory
    producer: Callable[[], EventPipeline]
    # The permissions-helper factory, or None on a platform that has no helper (Linux). None
    # never means "skip the helper": a platform that has one always carries it, and a step that
    # needs it calls `require_helper`.
    helper: HelperFactory | None
    host: Host
    session: UserSession
    timers: Timers
    services: frozenset[str]

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
