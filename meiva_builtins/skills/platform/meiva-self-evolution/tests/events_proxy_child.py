# meiva:note
# The child process of the env-proxy test: it reads the events API at the URL it is given through `collect.fetch_events_window` and prints how many rows came back. The test starts it with an environment that holds only a proxy variable aimed at a port nothing listens on, so a client that honored it could not reach the listener.
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from base.cluster.machine import GatewayAccess
from meiva_builtins.skills.tests.skill_script import load_skill_script

collect = load_skill_script("platform", "meiva-self-evolution", "scripts", "collect.py")
events_window = load_skill_script("platform", "meiva-self-evolution", "scripts", "events_window.py")

start = datetime(2026, 8, 13, tzinfo=UTC)
gateway = GatewayAccess(sys.argv[1], "s", False, None, None, False)
window = events_window.Window(start, start + timedelta(hours=1))
rows = collect.fetch_events_window("audit", window, gateway=gateway)
sys.stdout.write(f"{len(rows)}\n")
