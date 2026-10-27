# meiva:note
# The child process of `test_sdk_event_producer.py`: a process whose first plugin load happens
# inside a bound attachment. It detaches, makes one bare SDK call through the installation's
# client set and then leaves by `exit` (the interpreter's normal exit, which runs the exit hooks) or
# `hard` (`os._exit`, which runs none). Arguments: the repository root, then the mode.

from __future__ import annotations

import os
import sys
from typing import assert_never

from base.paths import meiva_home

__all__ = ["main"]


def main() -> None:
    root, mode = sys.argv[1], sys.argv[2]
    if mode not in ("exit", "hard"):
        raise ValueError(f"unknown exit mode {mode!r}")
    sys.path.insert(0, root)
    import meiva
    from base.agents.context import MeivaContext
    from base.agents.context.clients import ClientSet
    from base.agents.context.identity import AgentIdentity
    from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner
    from base.cluster.machine.configured import ConfiguredMachineIdentity
    from base.config import ConfigBoot
    from meiva.sdk_surface import metering, process_context
    from meiva.sdk_surface.installation import installed

    config = ConfigBoot()
    config.read_process_environment()
    attachment = ClientSet()
    process_context.bind_context(
        MeivaContext(
            clients=attachment,
            identity=AgentIdentity(agent_id=6111, owns_loop=False),
            meiva_home=meiva_home(),
        )
    )
    meiva.ensure_plugins_loaded(config=config, machine=ConfiguredMachineIdentity(config))
    installation = installed()
    if installation is None or installation.clients is None:
        raise RuntimeError("the plugin load installed no client set")
    attachment.close()
    process_context.unbind_context()
    sampling = fixed_sampling_owner()
    wrapped = metering.make_recorder(lambda: "done", "probe.exit", sampling, installation.clients)
    if wrapped() != "done":
        raise RuntimeError("the metered call changed its result")
    if mode == "hard":
        os._exit(0)
    elif mode == "exit":
        return
    else:
        assert_never(mode)


if __name__ == "__main__":
    main()
