"""Configuration, machine identity, helper and host access owned by one operator invocation."""

from __future__ import annotations

import os
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from base.clock import SYSTEM_TIMERS, Timers
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.host.permissions_helper.client import HelperFactory
from base.host.system.hostport import Host
from base.paths import repo_root
from base.sessions.helperproc import helper_for

__all__ = ["CommandRuntime", "Terminal", "UserSession"]


# meiva:note
# The invocation's random generator. Jitter and sampling are not secrets, so it is a plain
# `random.Random`, built once per command here and handed down through `CommandRuntime.rng`.
def _new_rng() -> random.Random:
    return random.Random()  # noqa: S311


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


@dataclass(frozen=True)
class CommandRuntime:
    """Keep one lazy identity through dispatch and embedded lifecycle legs."""

    config: ConfigBoot
    # meiva:note
    # This process's id, read once where the command is composed; the lifecycle lock and journal
    # name their holder by it.
    pid: int
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
    # The one random generator of this invocation. Jitter and sampling that no secret rides on
    # draw from it (`random.Random`, not `SystemRandom`); a command hands it down to the owners it
    # builds and never makes a second one.
    rng: random.Random
    # meiva:note
    # The checkout whose code runs this invocation (`base.paths.repo_root`), chosen here with the
    # other process facts. The entry's checkout gate has already accepted it for `home`.
    checkout: Path
    terminal: Terminal

    # meiva:note
    # Construct without reading configuration, identity or network inputs, but for the invoking
    # user's session and the process's terminal, which the process names once here. `home` is the
    # resolved home the entry settled on; it is neither created nor checked here.
    #
    # This is the one place the real permissions-helper factory, the real host, the real
    # timers, the real user session, the real checkout and the real terminal are chosen: the
    # helper (for the host's platform) only builds a client and reads the helper's port when a
    # macOS code path asks for one, and the host only answers when a step asks it to find or run
    # something.
    @classmethod
    def create(cls, home: Path) -> CommandRuntime:
        config = ConfigBoot()
        host = Host.system()
        return cls(
            config=config,
            pid=os.getpid(),
            home=home,
            machine=ConfiguredMachineIdentity(config),
            helper=helper_for(host.platform),
            host=host,
            session=UserSession.current(),
            timers=SYSTEM_TIMERS,
            rng=_new_rng(),
            checkout=repo_root(),
            terminal=Terminal.current(),
        )
