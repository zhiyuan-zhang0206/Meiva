"""Host cancellation diagnostics: bounded unwind and activity-clock reads.

These bounds limit diagnostic work after explicit cancellation. Graceful
maintenance waits for durable drain and fails on timeout; it does not use an
automatic SIGKILL deadline. Registered in base/deploy/timing.py::CLOCKS.
"""

from __future__ import annotations

# How long a cancelled turn gets to unwind before the host reports it as
# uncancellable and stops waiting (services/agent_runner/agent_host/dispatcher.py).
CANCEL_UNWIND_TIMEOUT_S = 5.0

# Ceiling on reading the stuck agents' activity clocks to enrich that report
# (services/agent_runner/agent_host/dispatcher.py). Small on purpose: diagnostic enrichment
# of a report already worth emitting without it, and the DB may be exactly what
# a wedged turn is stuck on.
CLOCK_READ_TIMEOUT_S = 2.0

# meiva:note
# How long a SIGTERMed agent-host waits for its running turns to settle at the restart
# each was given; a turn still running at the deadline falls through to the cancel path.
# Registered in base/deploy/timing.py::CLOCKS like the bounds above.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md
HOST_GRACEFUL_EXIT_DRAIN_S = 40.0

# meiva:note
# The longest an agent-host's whole SIGTERM exit may run: the drain, then the cancel
# unwind and its clock read, then resource join and ownership release. The roster
# declares it as the unit's stop ceiling, so root's TERM window derives from it.
HOST_STOP_CEILING_S = 55.0
