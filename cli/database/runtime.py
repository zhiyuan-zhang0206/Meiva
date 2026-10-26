"""Configuration, machine identity, helper and host access owned by one operator invocation."""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig, Timers
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.host.permissions_helper.client import HelperFactory
from base.host.system.hostport import Host
from base.paths import repo_root
from base.sessions.helperproc import helper_for

__all__ = ["CommandRuntime", "NewId", "Terminal", "UserSession", "delivered_config", "mint_id"]

# meiva:note
# A source of fresh identifiers (see `mint_id`): each call answers a new one.
type NewId = Callable[[], str]


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


# meiva:note
# The three streams a person sits at. A command that asks for a typed confirmation needs them as
# values: only a terminal on both ends can answer it.
@dataclass(frozen=True)
class Terminal:
    stdin: TextIO
    stdout: TextIO
    stderr: TextIO

    @property
    def interactive(self) -> bool:
        return self.stdin.isatty() and self.stdout.isatty()

    # meiva:note
    # This process's streams; `CommandRuntime.create` calls it once per invocation.
    @classmethod
    def current(cls) -> Terminal:
        return cls(sys.stdin, sys.stdout, sys.stderr)


# meiva:note
# The configuration owner with this process's startup delivery captured, once.
#
# An owner that was not prepared yet captures the delivery the process already made at startup
# instead of delivering it a second time (a second gateway fetch on a pure runner); a prepared
# owner already holds it. A command that hands its owner to code that reads configuration calls
# this at the verb's door.
def delivered_config(config: ConfigBoot) -> ConfigBoot:
    if not config.prepared:
        config.read_process_environment()
    return config


# meiva:note
# A fresh random identifier: 32 lowercase hexadecimal characters, the digits of a version 4 UUID.
# `CommandRuntime.create` is the one place the command line chooses it as its `new_id`.
def mint_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class CommandRuntime:
    """Keep one lazy identity through dispatch and embedded lifecycle legs."""

    config: ConfigBoot
    # meiva:note
    # The home this invocation operates, resolved once by the entry (`cli.main`), which reads the
    # same value for the init/start site and creates the directory: the runtime only holds it, so
    # nothing below the entry names the home a second way.
    home: Path
    machine: ConfiguredMachineIdentity
    # meiva:note
    # The permissions-helper factory, or None on a platform that has no helper (Linux). None is
    # only ever the answer to "does this platform have a helper": no setting, environment
    # variable or flag produces it on macOS, where the helper is mandatory.
    helper: HelperFactory | None
    host: Host
    session: UserSession
    timers: Timers
    # meiva:note
    # The wall clock of this invocation, over the same `timers`. A command asks it for the instant it
    # stamps or compares with (`clock.now()`, UTC) and passes that value down; the real one carries
    # the UTC zone because an instant has none to choose, and a command that formats times for a
    # person builds a clock from its configuration owner (`clock_config_from_boot`).
    clock: Clock
    # meiva:note
    # The checkout whose code runs this invocation (`base.paths.repo_root`), chosen here with the
    # other process facts. The entry's checkout gate has already accepted it for `home`.
    checkout: Path
    terminal: Terminal
    # meiva:note
    # Mints a fresh identifier: the idempotency key of a gateway request, the name of a new
    # record. A command asks its runtime for each identifier it needs, so a test hands in the
    # sequence it wants.
    new_id: NewId

    # meiva:note
    # Construct without reading configuration, identity or network inputs, but for the invoking
    # user's session and the process's terminal, which the process names once here. `home` is the
    # resolved home the entry settled on; it is neither created nor checked here.
    #
    # This is the one place the real permissions-helper factory, the real host, the real
    # timers and clock, the real user session, the real checkout, the real terminal and the real
    # identifier source are chosen: the helper (for the host's platform) only builds a client and reads the helper's port when a
    # macOS code path asks for one, and the host only answers when a step asks it to find or run
    # something.
    @classmethod
    def create(cls, home: Path) -> CommandRuntime:
        config = ConfigBoot()
        host = Host.system()
        return cls(
            config=config,
            home=home,
            machine=ConfiguredMachineIdentity(config),
            helper=helper_for(host.platform),
            host=host,
            session=UserSession.current(),
            timers=SYSTEM_TIMERS,
            clock=Clock(
                ClockConfig("UTC", None, message_timestamp_weekday=False), timers=SYSTEM_TIMERS
            ),
            checkout=repo_root(),
            terminal=Terminal.current(),
            new_id=mint_id,
        )
