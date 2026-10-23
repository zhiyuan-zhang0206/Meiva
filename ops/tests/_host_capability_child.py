# meiva:note
# Exercise host capability gates with the environment fixed before application import.

from __future__ import annotations

import json
import sys

from base.cluster.machine import MachineIdentity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.host.system.tests.fake_commands import fake_host
from base.packages.plugins import mcp_enabled
from base.paths import meiva_home
from ops.host_config import config_read_op
from ops.inventory import inventory_read_op, inventory_write_op


def main(operation: str) -> None:
    host = fake_host(programs={"npx": "/test/npx"})
    if operation == "config":
        authority = ConfigAuthority(settings, settings, meiva_home() / ".env")
        result = config_read_op(authority=authority, local_machine="test-machine", host=host)
        sys.stdout.write(result.model_dump_json())
    elif operation == "inventory":
        machine = MachineIdentity(
            name=lambda: "test-machine",
            role=lambda: frozenset({"agent-runner"}),
            host=lambda: "localhost",
            description=lambda: None,
        )
        inventory = inventory_read_op(local_machine=machine, host=host)
        enable = inventory_write_op(
            plugins={}, mcp_servers={"that": True}, local_machine=machine, host=host
        )
        after_enable = mcp_enabled.read_enabled()
        disable = inventory_write_op(
            plugins={}, mcp_servers={"that": False}, local_machine=machine, host=host
        )
        sys.stdout.write(
            json.dumps(
                {
                    "inventory": inventory.model_dump(mode="json"),
                    "enable": enable.model_dump(mode="json"),
                    "after_enable": after_enable,
                    "disable": disable.model_dump(mode="json"),
                }
            )
        )
    else:
        raise ValueError(f"unknown host-capability operation: {operation!r}")


if __name__ == "__main__":
    main(sys.argv[1])
