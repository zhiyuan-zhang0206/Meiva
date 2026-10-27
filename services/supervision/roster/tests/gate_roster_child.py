# meiva:note
# A child process that evaluates the real roster gates under the environment its parent chose
# and prints `{session: reason or null}` as JSON. The gates read process settings, the home and
# the environment, so each case needs a process of its own; the host is a scripted one with no
# Chrome, holding the environment of this process (the parent builds it without a display).

from __future__ import annotations

import json
import sys

from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.host.system.tests.fake_commands import fake_host
from base.paths import meiva_home
from services.supervision.roster import spec


def main(roles_csv: str) -> None:
    config = ConfigBoot()
    config.read_process_environment()
    machine = ConfiguredMachineIdentity(config)
    machine.set(role=roles_csv)
    rows = spec.services_for_capabilities_annotated(
        frozenset(roles_csv.split(",")),
        machine=machine,
        host=fake_host(environ=config.process_environment()),
        home=meiva_home(),
    )
    sys.stdout.write(json.dumps({service.session: reason for service, reason in rows}) + "\n")


if __name__ == "__main__":
    main(sys.argv[1])
