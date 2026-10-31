# meiva:note
# Booting an external process is the same work whoever borrowed the identity: read the launch
# environment, resolve the machine, build the database factory, and (only when nothing is bound
# yet) own a client set. The clock comes from here too because an independent attachment has no
# process clock to inherit. The takeover and the harness differ in *what proves the identity*;
# they must not differ in how the process around it is built, or a fix to one boot path silently
# misses the other. This module owns that half; the SDK process bootstrap it builds on stays in
# `meiva/boot.py`.
# meiva:link meiva/docs/boot-contracts.meiva.okf.md
# meiva:link base/agents/context/external_attachment/docs/external-attachment.meiva.okf.md

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from base.agents.context import BusFactory, MeivaContext, deferred_event_bus
from base.agents.context.clients import ClientSet, DatabaseFactory
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.packages.plugins.loading import StateGate
from meiva.boot import clients_over, database_factory, initialize_context


# meiva:note
# What an external process needs before it can act as an agent, when it borrows a bound context:
# the machine and the database factory of that context. The configuration, the clients and the
# clock stay the context's own, so this boot holds none of them.
@dataclass(frozen=True, slots=True)
class BorrowedBoot:
    machine: MachineIdentityReader
    database: DatabaseFactory


# meiva:note
# What an external process needs before it can act as an agent, when nothing is bound yet and it
# owns its roots: the prepared configuration, the machine, the database factory the clients share,
# the clients themselves, the clock an independent attachment has no process clock to inherit, the
# two homes it reads once (the account's and the Meiva home), the operating system's random
# generator and the factory of the event bus over its configuration. An attachment is a separate process whose identifiers must stay unique across the
# processes of one agent, so it draws from the system generator rather than a seeded-at-start one.
@dataclass(frozen=True, slots=True)
class OwnedBoot:
    config: ConfigBoot
    machine: ConfiguredMachineIdentity
    database: DatabaseFactory
    clients: ClientSet
    clock: Clock
    user_home: Path
    meiva_home: Path
    rng: random.SystemRandom
    bus_factory: BusFactory

    # meiva:note
    # The context an independent attachment binds before it has any: the owned clients, the clock,
    # the machine, the homes, the random generator and the lazily built event bus of this boot.
    def new_context(self) -> MeivaContext:
        clock = self.clock
        return MeivaContext(
            clients=self.clients,
            clock_factory=lambda: clock,
            bus_factory=self.bus_factory,
            machine=self.machine,
            user_home=self.user_home,
            meiva_home=self.meiva_home,
            rng=self.rng,
        )


type ExternalBoot = BorrowedBoot | OwnedBoot


def borrow_external_process(bound: MeivaContext) -> BorrowedBoot:
    return BorrowedBoot(machine=bound.require_machine(), database=bound.clients.database)


def own_external_process(config: ConfigBoot) -> OwnedBoot:
    if not config.prepared:
        config.read_process_environment()
    from base.paths.home import resolve_meiva_home

    machine = ConfiguredMachineIdentity(config)
    database = database_factory(config=config, machine=machine)
    rng = random.SystemRandom()
    return OwnedBoot(
        config=config,
        machine=machine,
        database=database,
        clients=clients_over(config, machine, database),
        clock=Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
        user_home=Path.home(),
        meiva_home=resolve_meiva_home(),
        rng=rng,
        bus_factory=deferred_event_bus(config, rng=rng, timers=SYSTEM_TIMERS),
    )


# meiva:note
# Bring up the plugin surface of an external process around its attachment: an owned boot hands
# the load its configuration and clients, a borrowed one leaves both to the context it borrows.
def initialize_external(
    context: MeivaContext, boot: ExternalBoot, *, validate_state: StateGate | None
) -> None:
    match boot:
        case OwnedBoot():
            initialize_context(
                context,
                surface=False,
                config=boot.config,
                clients=boot.clients,
                validate_state=validate_state,
            )
        case BorrowedBoot():
            initialize_context(
                context,
                surface=False,
                config=None,
                clients=None,
                validate_state=validate_state,
            )
        case _:
            assert_never(boot)
