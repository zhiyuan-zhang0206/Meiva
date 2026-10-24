# meiva:note
# Child process for the headless-display test: a Linux host with no display refuses a display
# requirement.


import sys

import base.packages.mcp_config as cfg
from base.host.system.hostport import Platform
from base.host.system.tests.fake_commands import fake_host

host = fake_host(platform=Platform("linux", "x86_64", "/usr/bin/python3"))
try:
    cfg.assert_requirements({"requires": {"display": True}}, host=host)
except cfg.MCPError as exc:
    sys.stdout.write(str(exc))
