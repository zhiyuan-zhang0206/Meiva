"""Configuration, machine identity, helper and host access owned by one operator invocation."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from base.clock import SYSTEM_TIMERS, Timers
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.host.permissions_helper.client import HelperFactory
from base.host.system.hostport import Host
from base.paths.home import resolve_meiva_home
from base.sessions.helperproc import helper_for

__all__ = ["CommandRuntime", "UserSession"]


# meiva:note
# The invoking user's session, read once where the operator's command is composed.
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
    # This process's session; `CommandRuntime.create` calls it once per invocation.
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
class CommandRuntime:
    """Keep one lazy identity through dispatch and embedded lifecycle legs."""

    config: ConfigBoot
    # meiva:note
    # The home this invocation operates, resolved: the entry's init/start site reads the same
    # pure `resolve_meiva_home().resolve()`, so the two never name one home two ways.
    home: Path
    machine: MachineIdentity
    # meiva:note
    # The permissions-helper factory, or None on a platform that has no helper (Linux). None is
    # only ever the answer to "does this platform have a helper": no setting, environment
    # variable or flag produces it on macOS, where the helper is mandatory.
    helper: HelperFactory | None
    host: Host
    session: UserSession
    timers: Timers

    # meiva:note
    # Construct without reading configuration, identity or network inputs, but for the home
    # path and the invoking user's session, which the process names once here.
    #
    # This is the one place the real permissions-helper factory, the real host, the real
    # timers and the real user session are chosen: the helper (for the host's platform) only
    # builds a client and reads the helper's port when a macOS code path asks for one, and the
    # host only answers when a step asks it to find or run something. The home is only read,
    # never created or checked: creating it is the entry's business (`cli.main`).
    @classmethod
    def create(cls) -> CommandRuntime:
        config = ConfigBoot()
        host = Host.system()
        return cls(
            config=config,
            home=resolve_meiva_home().resolve(),
            machine=machine_from_boot(config),
            helper=helper_for(host.platform),
            host=host,
            session=UserSession.current(),
            timers=SYSTEM_TIMERS,
        )
