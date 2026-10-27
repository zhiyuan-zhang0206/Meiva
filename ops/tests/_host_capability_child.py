# meiva:note
# Read the host config in a process whose environment was fixed before application import, so the
# browser capability gate sees the display and Chrome binary the test chose. Prints the
# `ConfigReadResult` as JSON.

from __future__ import annotations

import sys

from base.config import settings
from base.config.service_read import ConfigAuthority
from base.host.system.tests.fake_commands import fake_host
from base.paths import meiva_home
from ops.host_config import config_read_op


def main() -> None:
    authority = ConfigAuthority(settings, settings, meiva_home() / ".env")
    result = config_read_op(
        authority=authority,
        local_machine="test-machine",
        host=fake_host(programs={"npx": "/test/npx"}),
        home=meiva_home(),
    )
    sys.stdout.write(result.model_dump_json())


if __name__ == "__main__":
    main()
