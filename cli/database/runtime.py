"""Configuration, machine identity, helper and host access owned by one operator invocation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from base.clock import SYSTEM_TIMERS, Timers
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.host.permissions_helper.client import HelperFactory
from base.host.system.hostport import Host
from base.paths import meiva_home
from base.sessions.helperproc import helper_for

__all__ = ["CommandRuntime"]


@dataclass(frozen=True)
class CommandRuntime:
    """Keep one lazy identity through dispatch and embedded lifecycle legs."""

    config: ConfigBoot
    # meiva:note
    # The home this invocation operates: read once here, handed down as a value.
    home: Path
    machine: MachineIdentity
    # The permissions-helper factory, or None on a platform that has no helper (Linux). None is
    # only ever the answer to "does this platform have a helper": no setting, environment
    # variable or flag produces it on macOS, where the helper is mandatory.
    helper: HelperFactory | None
    host: Host
    timers: Timers

    # meiva:note
    # Construct without reading configuration, identity or network inputs, but for the
    # home path, which the operator's environment names once here.
    #
    # This is the one place the real permissions-helper factory, the real host and the real
    # timers are chosen: the helper (for the host's platform) only builds a client and reads the
    # helper's port when a macOS code path asks for one, and the host only answers when a step
    # asks it to find or run something.
    @classmethod
    def create(cls) -> CommandRuntime:
        config = ConfigBoot()
        host = Host.system()
        return cls(
            config=config,
            home=meiva_home(),
            machine=machine_from_boot(config),
            helper=helper_for(host.platform),
            host=host,
            timers=SYSTEM_TIMERS,
        )
