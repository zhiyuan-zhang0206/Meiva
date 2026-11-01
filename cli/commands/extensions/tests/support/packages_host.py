# meiva:note
# The host a case hands the package refresh: the production runner (real git against the checkout
# the case wrote) and the environment its configuration carries, never the machine running the
# suite. One definition so each case names the same machine.
from __future__ import annotations

from base.config import ConfigBoot
from base.host.proc import run_bounded
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host

__all__ = ["packages_host"]


def packages_host(config: ConfigBoot) -> Host:
    return fake_host(commands=run_bounded, environ=config.process_environment())
