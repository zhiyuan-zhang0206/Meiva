# meiva:note
# Exercise the inventory ops in a process whose environment was fixed before application import,
# so the display requirement of an MCP server sees the display the test chose. Prints the read,
# the refused enable, the overlay after it and the allowed disable as JSON.

from __future__ import annotations

import json
import sys

from base.cluster.machine import MachineFacts
from base.host.system.tests.fake_commands import fake_host
from base.packages.plugins import mcp_enabled
from ops.inventory import inventory_read_op, inventory_write_op


def main() -> None:
    host = fake_host(programs={"npx": "/test/npx"})
    machine = MachineFacts.of(
        name="test-machine",
        role=frozenset({"agent-runner"}),
        host="localhost",
        description=None,
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


if __name__ == "__main__":
    main()
